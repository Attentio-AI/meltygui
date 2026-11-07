"""One relative position API for state, calls, defaults and saved sessions."""
import pytest

from meltygui.core.rendering.gui_prototype import gui, _bind_native, _new_native
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from native_gui_support import native_frame


def test_axes_resolve_independently_and_inject_the_resolved_values():
    cache = RetainedGui(graphics=False)
    seen = []
    @cache.gui(left=10)
    def view(input_value, left=2, top=3, draw_state=None):
        seen.append((left, top, draw_state))
    try:
        with cache.frame(): view(0, left=5)
        assert seen[-1][:2] == (5, 3)
        state = seen[-1][2]
        state.top = 40
        with cache.frame(): view(1, left=-7)
        assert seen[-1][:2] == (-7, 40)
        with cache.frame(): view(2)
        assert seen[-1][:2] == (10, 40)
        assert seen[-1][2] is state
        assert 'window_pos' not in state.as_dict()
    finally:
        cache.close()


def test_initial_and_declared_defaults_are_per_axis():
    cache = RetainedGui(graphics=False)
    @cache.gui(initial={'left': 12, 'top': 18})
    def view(input_value, left=0, top=0, draw_state=None): return False, draw_state
    try:
        with cache.frame(): state = view(None)[1]
        assert (state.left, state.top) == (12, 18)
        state.left = 31
        with cache.frame(): view(1, top=50)
        assert (state.left, state.top) == (31, 50)
        with cache.frame(): view(2)
        assert (state.left, state.top) == (31, 18)
    finally:
        cache.close()


def test_signature_position_edits_apply_without_recreating_state():
    cache = RetainedGui(graphics=False)
    @cache.gui
    def view(input_value, left=5, draw_state=None): return False, draw_state
    def changed(input_value, left=15, draw_state=None): return False, draw_state
    try:
        with cache.frame(): state = view(None)[1]
        view.__wrapped__.__code__ = changed.__code__
        view.__wrapped__.__defaults__ = changed.__defaults__
        cache.flush()
        assert state.left == 15
        assert next(iter(cache.states.values())) is state
        state.left = 50
        def changed_again(input_value, left=25, draw_state=None): return False, draw_state
        view.__wrapped__.__code__ = changed_again.__code__
        view.__wrapped__.__defaults__ = changed_again.__defaults__
        cache.flush()
        assert state.left == 50
    finally:
        cache.close()


def test_legacy_saved_tuple_migrates_values_and_authored_axes():
    from meltygui.core.rendering._gui_native import NativeState
    state = NativeState()
    state.__setstate__(({'unique': 9, 'dlt_count': 0, 'window_pos': (11, 22)}, {},
                        {'window_pos': (33, 44)}))
    assert (state.left, state.top) == (11, 22)
    state.configure({'left': 0, 'top': 0}, {}, {})
    assert (state.left, state.top) == (33, 44)
    assert 'window_pos' not in state.as_dict()
    # Explicit new names win if a record contains both schemas.
    state = NativeState()
    state.__setstate__(({'window_pos': (11, 22), 'left': 99}, {}, {}))
    assert (state.left, state.top) == (99, 22)


@pytest.mark.parametrize('arguments', [{'window_pos': (1, 2)}, {'initial': {'window_pos': (1, 2)}}])
def test_old_call_spelling_fails_clearly(arguments):
    with pytest.raises(TypeError, match='left and top'):
        gui(**arguments)
    cache = RetainedGui(graphics=False)
    @cache.gui
    def view(input_value): pass
    native = _new_native(graphics=False)
    immediate = _bind_native(native, view.__wrapped__)
    try:
        with pytest.raises(TypeError, match='left and top'): view(None, **arguments)
        with native_frame(native):
            with pytest.raises(TypeError, match='left and top'): immediate(None, **arguments)
    finally:
        cache.close()
        native.clear()


def test_uncached_positioning_uses_the_current_content_space():
    from meltygui import imgui
    from conftest import begin_frame, end_frame
    native = _new_native(graphics=True)
    seen = []
    @_bind_native(native)
    def view(input_value, left=None, top=None, draw_state=None):
        seen.append((tuple(imgui.get_cursor_screen_pos()), left, top, draw_state.left, draw_state.top))
        imgui.dummy(10, 10)
    begin_frame()
    imgui.begin('position API')
    try:
        with native_frame(native):
            imgui.set_cursor_screen_pos((40, 70))
            view(None, left=5)
            imgui.set_cursor_screen_pos((40, 70))
            view(None, top=9)
            imgui.set_cursor_screen_pos((40, 70))
            view(None, left=None, top=9, initial={'left': 20})
        assert seen == [((5, 70), 5, 70, 5, 70), ((40, 9), 40, 9, 40, 9),
                        ((40, 9), 40, 9, 40, 9)]
    finally:
        imgui.end()
        end_frame()
        native.clear()


def test_cached_left_only_preserves_flow_top_and_moves_cached_pixels(gl_context):
    import numpy as np
    from meltygui import imgui
    from test_retained_gui_gpu import pixels
    cache = RetainedGui()
    calls, ids = [], {}
    @cache.gui(width=20, height=20)
    def child(input_value, left=None, top=None, draw_state=None, cache=None):
        calls.append((left, top, tuple(imgui.get_cursor_screen_pos())))
        ids['child'] = cache.current
        imgui.get_window_draw_list().add_rect_filled(0, 0, 10, 10, 0xFF0000FF)
    @cache.gui(width=100, height=80)
    def parent(input_value, cache=None):
        ids['parent'] = cache.current
        imgui.set_cursor_screen_pos((40, 12))
        child(None, left=5)
    try:
        with cache.frame(): parent(None, left=30, top=40)
        assert calls == [(5, 12, (0, 0))]
        assert cache.position(ids['child']) == (35, 52)
        texture = cache.gpu.texture(ids['parent'])
        image = np.flipud(pixels(texture, 100, 80))
        assert tuple(image[13, 6]) == (255, 0, 0, 255)
        cache.states[ids['child']].left = 25
        cache.flush()
        image = np.flipud(pixels(texture, 100, 80))
        assert image[13, 6, 3] == 0
        assert tuple(image[13, 26]) == (255, 0, 0, 255)
        assert len(calls) == 1
    finally:
        cache.close()
