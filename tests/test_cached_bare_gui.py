"""Run with the isolated bare-wrapper extension loaded before pytest imports."""
import gc
from concurrent.futures import ThreadPoolExecutor
import weakref

import pytest
from meltygui.core.rendering import _gui_native

pytestmark = pytest.mark.skipif(not hasattr(_gui_native, 'CachedBareGui'),
                                reason='Requires isolated cached-bare experiment build')


class Bridge:
    def __init__(self):
        self.captures = []
        self.placements = []

    def capture(self, key, root, function, value, arguments):
        self.captures.append((key, root))
        return function(value, **arguments)

    def place(self, key, arguments, root, dirty):
        self.placements.append((key, root, dirty))


def test_hit_skips_body_and_invalidation_preserves_mutable_identity():
    bridge, value, calls = Bridge(), [], []
    def body(input_value, **kwargs):
        calls.append(kwargs)
        return False, input_value
    wrapper = _gui_native.CachedBareGui(body, bridge)
    first = wrapper(value, key=0, width=128)
    assert wrapper(value, key=0, width=128) is first
    assert len(calls) == 1 and first[1] is value
    value.append('edited')
    wrapper.invalidate()
    assert wrapper(value, key=0, width=128)[1] is value
    assert len(calls) == 2 and wrapper.count() == 1
    assert bridge.placements == [(0, True, True), (0, True, False), (0, True, True)]
    wrapper.clear()
    assert wrapper.count() == 0


def test_recursive_views_have_distinct_records_and_no_callback_lock():
    bridge = Bridge()
    def body(input_value, **kwargs):
        if input_value:
            wrapper(input_value-1, key=input_value-1)
        return False, input_value
    wrapper = _gui_native.CachedBareGui(body, bridge)
    assert wrapper(3, key=3) == (False, 3)
    assert wrapper.count() == 4
    assert bridge.captures == [(3, True), (2, False), (1, False), (0, False)]
    wrapper(3, key=3)
    assert len(bridge.captures) == 4


def test_failure_unwinds_depth_and_invalidates_partial_capture():
    bridge, failing = Bridge(), [True]
    error = ValueError('body failed')
    def body(input_value, **kwargs):
        if input_value:
            wrapper(0, key=0)
            if failing[0]:
                raise error
        return False, input_value
    wrapper = _gui_native.CachedBareGui(body, bridge)
    with pytest.raises(ValueError) as caught:
        wrapper(1, key=1)
    assert caught.value is error
    failing[0] = False
    wrapper(1, key=1)
    assert bridge.captures == [(1, True), (0, False), (1, True), (0, False)]


def test_invalidation_during_capture_is_not_lost():
    bridge, calls = Bridge(), []
    def body(input_value, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            wrapper.invalidate()
        return False, input_value
    wrapper = _gui_native.CachedBareGui(body, bridge)
    for _ in range(3):
        wrapper(None, key=0)
    assert len(calls) == 2


def test_requires_explicit_key_and_rejects_wrong_render_thread():
    wrapper = _gui_native.CachedBareGui(lambda value, **kwargs: (False, value), Bridge())
    with pytest.raises(ValueError, match='key'):
        wrapper(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match='owning thread'):
            pool.submit(wrapper, None, key=0).result()


def test_gc_collects_recursive_wrapper_cycle():
    def make():
        bridge = Bridge()
        def body(value, **kwargs):
            return wrapper.count(), value
        reference = weakref.ref(body)
        wrapper = _gui_native.CachedBareGui(body, bridge)
        wrapper(None, key=0)
        return reference
    reference = make()
    gc.collect()
    assert reference() is None


def test_clear_allows_reentrant_result_destructor():
    observations = []
    class Result:
        def __del__(self):
            observations.append(wrapper.count())
    wrapper = _gui_native.CachedBareGui(lambda value, **kwargs: Result(), Bridge())
    wrapper(None, key=0)
    wrapper.clear()
    assert observations == [0]


def test_real_capture_restores_context_after_nested_failure_and_closes_resources(gl_context):
    from conftest import begin_frame, end_frame
    from cached_bare_gui import CaptureBridge
    bridge = CaptureBridge()
    imgui = bridge.imgui
    fail = [True]
    def body(value, **arguments):
        imgui.get_window_draw_list().add_rect_filled(0, 0, 8, 2, 0xffffffff)
        if arguments['key'] == 0:
            wrapper(value, key=1, left=0., top=0., width=128., height=256.)
        elif fail[0]:
            raise ValueError('nested capture failed')
        return False, value
    wrapper = _gui_native.CachedBareGui(body, bridge)
    begin_frame()
    imgui.begin('cached bare failure test')
    try:
        previous = imgui.get_current_context()
        keepalive = tuple(previous._keepalive_cache)
        with pytest.raises(ValueError, match='nested capture'):
            wrapper(None, key=0, left=0., top=0., width=128., height=512.)
        assert imgui.get_current_context() is previous
        assert tuple(previous._keepalive_cache) == keepalive
        assert bridge.stack == []
        fail[0] = False
        assert wrapper(None, key=0, left=0., top=0., width=128., height=512.) == (False, None)
        assert wrapper.count() == len(bridge.captures) == 2
        wrapper.clear()
        bridge.close()
        assert not bridge.captures and not bridge.texture_refs
        assert bridge.gpu.stats()[2:] == (0, 0)
    finally:
        wrapper.clear()
        bridge.close()
        imgui.end()
        end_frame()


def test_fixed_body_forwards_only_explicit_geometry_and_preserves_result():
    value, state = [], object()
    seen = []
    def body(input_value, left, top, width, height, key):
        seen.append((input_value, left, top, width, height, key))
        return True, input_value
    result = _gui_native.fixed_body(body, value,
        {'left': 2., 'top': 3., 'width': 128., 'height': 64.,
         'min_height': 0., 'max_height': 8192., 'draw_state': object()}, state)
    assert result == (True, value, state)
    assert result[1] is value and result[2] is state
    assert seen == [(value, 2., 3., 128., 64., None)]
    with pytest.raises(ValueError, match='height'):
        _gui_native.fixed_body(body, value, {'width': 128.}, state)
