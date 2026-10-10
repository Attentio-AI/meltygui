"""Cocoa window movement must not become pointer movement during a drag."""
from types import SimpleNamespace

from meltygui.core.graphics.overlay_renderer import SplitOverlayRenderer
from meltygui.core.windowing import melty_windows, geometry_feed, wayland_move, window_api
from meltygui.core.diagnostics import edge_motion_guard
from meltygui.core.melty import Melty


def test_cocoa_drag_uses_screen_motion_through_move_hold_and_reversal(monkeypatch):
    window = object()
    origin = [400., 300.]
    renderer = SimpleNamespace(window=window, _slide_screen_origin=None,
                               _slide_native_sample=None)
    io = SimpleNamespace(mouse_down=[False, True, False], mouse_pos=(100., 100.))
    monkeypatch.setattr(wayland_move, 'relative_motion_available', lambda: False)
    monkeypatch.setattr(geometry_feed, 'frame_rect', lambda: None)
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda w: w is window)
    monkeypatch.setitem(window_api.__dict__, 'get_window_pos', lambda w: tuple(origin))
    monkeypatch.setattr(edge_motion_guard, '_surface', lambda: SimpleNamespace(impl=renderer))
    monkeypatch.setattr(Melty, 'get_latest_mouse', lambda: io.mouse_pos)
    for screen, position in [((500., 400.), (400., 300.)),
                             ((510., 410.), (390., 290.)),
                             ((510., 410.), (370., 270.)),
                             ((505., 405.), (380., 280.)),
                             ((500., 400.), (400., 300.))]:
        origin[:] = position
        io.mouse_pos = (screen[0]-origin[0], screen[1]-origin[1])
        SplitOverlayRenderer._cancel_surface_slide(renderer, io)
        assert io.mouse_pos == (screen[0]-400., screen[1]-300.)
        assert edge_motion_guard._pointer(tuple(origin)) == screen
    io.mouse_down = [False]*3
    io.mouse_pos = (32., 45.)
    SplitOverlayRenderer._cancel_surface_slide(renderer, io)
    assert io.mouse_pos == (32., 45.)
    assert renderer._slide_screen_origin is None


def test_unowned_cocoa_window_keeps_local_pointer(monkeypatch):
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda w: False)
    monkeypatch.setattr(wayland_move, 'relative_motion_available', lambda: False)
    monkeypatch.setattr(geometry_feed, 'frame_rect', lambda: None)
    renderer = SimpleNamespace(window=object(), _slide_screen_origin=None,
                               _slide_native_sample=None)
    io = SimpleNamespace(mouse_down=[False, True, False], mouse_pos=(20., 30.))
    SplitOverlayRenderer._cancel_surface_slide(renderer, io)
    assert io.mouse_pos == (20., 30.)


def test_release_keeps_final_drag_motion_separate_from_hover(monkeypatch):
    origin=[400.,300.]
    renderer=SimpleNamespace(window=object(),_slide_screen_origin=None,
                             _slide_native_sample=None)
    io=SimpleNamespace(mouse_down=[False,True,False],mouse_pos=(100.,100.))
    monkeypatch.setattr(wayland_move,'relative_motion_available',lambda:False)
    monkeypatch.setattr(melty_windows,'drag_origin',lambda window:tuple(origin))
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    origin[:]=[350.,250.]
    io.mouse_pos=(100.,100.)
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    assert renderer.drag_mouse_pos==(50.,50.)
    # The release brings another 5px of hand motion in the translated surface.
    io.mouse_down=[False]*3
    io.mouse_pos=(95.,95.)
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    assert renderer.drag_mouse_pos==(45.,45.)
    assert io.mouse_pos==(95.,95.)
    assert renderer._slide_screen_origin is None
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    assert renderer.drag_mouse_pos==(95.,95.)


def test_relative_pointer_release_keeps_final_drag_motion(monkeypatch):
    renderer=SimpleNamespace(window=object(),_slide_screen_origin=None,
                             _slide_native_sample=None,_slide_base=None,
                             _slide_last=None,SLIDE_DEADBAND=1.5)
    io=SimpleNamespace(mouse_down=[False,True,False],mouse_pos=(100.,100.))
    relative=[0.,0.]
    monkeypatch.setattr(wayland_move,'relative_motion_available',lambda:True)
    monkeypatch.setattr(wayland_move,'relative_motion_total',lambda:tuple(relative))
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    relative[:]=[-50.,-50.]
    io.mouse_pos=(110.,110.)
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    assert renderer.drag_mouse_pos==(50.,50.)
    relative[:]=[-55.,-55.]
    io.mouse_pos=(105.,105.)
    io.mouse_down=[False]*3
    SplitOverlayRenderer._cancel_surface_slide(renderer,io)
    assert renderer.drag_mouse_pos==(45.,45.)
    assert io.mouse_pos==(105.,105.)
    assert renderer._slide_base is None


def test_cocoa_regrab_between_frames_reanchors_at_new_press(monkeypatch):
    from meltygui.core.input.input_handler import InputHandler
    handler = InputHandler()
    monkeypatch.setattr(Melty, 'event_handler', handler)
    origin = [400., 300.]
    window = object()
    renderer = SimpleNamespace(window=window, _slide_screen_origin=None,
                               _slide_native_sample=None)
    io = SimpleNamespace(mouse_down=[False, True, False], mouse_pos=(100., 100.))
    monkeypatch.setattr(wayland_move, 'relative_motion_available', lambda: False)
    monkeypatch.setattr(geometry_feed, 'frame_rect', lambda: None)
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda w: True)
    monkeypatch.setitem(window_api.__dict__, 'get_window_pos', lambda w: tuple(origin))
    handler.feed_down('right_mouse', 100, 100, t=10.)
    SplitOverlayRenderer._cancel_surface_slide(renderer, io)
    origin[:] = [300., 250.]
    io.mouse_pos = (200., 150.)
    SplitOverlayRenderer._cancel_surface_slide(renderer, io)
    assert io.mouse_pos == (100., 100.)
    handler.feed_up('right_mouse', 200, 150, t=11.)
    handler.feed_down('right_mouse', 200, 150, t=12.)
    # No rendered frame observed mouse_down=False.
    io.mouse_pos = (200., 150.)
    SplitOverlayRenderer._cancel_surface_slide(renderer, io)
    assert io.mouse_pos == (200., 150.)
    assert renderer._slide_screen_origin == (300., 250.)


def test_chord_press_does_not_restart_active_pointer_gesture():
    from meltygui.core.input.input_handler import InputHandler
    handler = InputHandler()
    handler.feed_down('right_mouse', 100, 100, t=10.)
    token = handler.pointer_press_token()
    handler.feed_down('left_mouse', 100, 100, t=11.)
    assert handler.pointer_press_token() == token
    handler.feed_up('left_mouse', 100, 100, t=12.)
    assert handler.pointer_press_token() == token
    handler.feed_up('right_mouse', 100, 100, t=13.)
    assert handler.pointer_press_token() is None


def test_handle_regrab_does_not_subtract_previous_gesture_total(monkeypatch):
    from meltygui.core.input.input_handler import InputHandler
    from meltygui.core.layout.column_core import _drag_inc
    handler = InputHandler()
    monkeypatch.setattr(Melty, 'event_handler', handler)
    view = SimpleNamespace()
    handler.feed_down('left_mouse', 100, 100, t=10.)
    assert _drag_inc(view, 'divider', SimpleNamespace(total_dx=80.)) == 80.
    handler.feed_up('left_mouse', 180, 100, t=11.)
    handler.feed_down('left_mouse', 180, 100, t=12.)
    assert _drag_inc(view, 'divider', SimpleNamespace(total_dx=5.)) == 5.
    assert _drag_inc(view, 'divider', SimpleNamespace(total_dx=8.)) == 3.


def test_queued_release_cannot_clear_new_press_baseline(monkeypatch):
    from meltygui.core.input import input_handler
    handler = input_handler.InputHandler()
    monkeypatch.setattr(input_handler, '_BUTTON_PROBE', {'fn': None})
    monkeypatch.setattr(Melty, 'event_handler', handler)
    pointer = [100., 100.]
    monkeypatch.setattr(Melty, 'get_latest_mouse', lambda: tuple(pointer))
    handler.register_hovered('handle', ['right_mouse_drag', 'right_mouse_drag_released'])
    handler.feed_down('right_mouse', 100, 100, t=10.)
    pointer[:] = [180., 100.]
    events = handler.process_frame()[0]
    assert events['handle']['right_mouse_drag'].total_dx == 80.
    handler.begin_frame()
    handler.register_hovered('handle', ['right_mouse_drag', 'right_mouse_drag_released'])
    handler.feed_up('right_mouse', 185, 100, t=11.)
    handler.feed_down('right_mouse', 400, 100, t=12.)
    pointer[:] = [405., 100.]
    events = handler.process_frame()[0]['handle']
    assert events['right_mouse_drag'].total_dx == 5.
    assert events['right_mouse_drag_released'].total_dx == 85.
    assert handler.pointer_press_token() == (12., 'right_mouse')
