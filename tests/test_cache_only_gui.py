"""Full-cache annotation tests, using the isolated native CacheRenderer build."""
import pytest
from meltygui.core.rendering import _gui_native

pytestmark = pytest.mark.skipif(not hasattr(_gui_native, 'CacheRenderer'),
                                reason='Requires isolated cache annotation build')


@pytest.fixture
def cache(gl_context):
    from cache_only_gui import CacheOnlyRetained
    cache = CacheOnlyRetained()
    cache.calibration.enabled = False
    yield cache
    cache.close()


def test_explicit_forwarding_has_no_injected_state_or_framework_keywords(cache):
    seen = []
    @cache.gui(width=64, height=32)
    def body(input_value, custom=7, **kwargs):
        seen.append((input_value, custom, kwargs))
        return False, input_value
    value = []
    assert body(value, key='body', custom=9) == (False, value)
    assert seen == [(value, 9, {})]
    body(value, key='body', custom=9)
    assert len(seen) == 1
    node = next(iter(cache.records))
    assert isinstance(cache.records[node][1].__gui_native__, _gui_native.CacheRenderer)
    assert cache.states[node].get('custom') is None


def test_nested_same_key_preserves_parent_identity_and_independent_invalidation(cache):
    from meltygui import imgui
    ids, calls = {}, []
    @cache.gui(width=64, height=16)
    def leaf(input_value):
        ids[input_value] = cache.current
        calls.append(input_value)
        imgui.get_window_draw_list().add_rect_filled(0, 0, 8, 2, 0xffffffff)
        return False, input_value
    @cache.gui(width=64, height=32)
    def branch(input_value, **kwargs):
        leaf(input_value, key='same-child-key')
        return False, input_value
    @cache.gui(width=64, height=64)
    def root(input_value):
        branch('a', key='a')
        branch('b', key='b', top=32.)
        return False, input_value
    root(None)
    assert ids['a'] != ids['b']
    assert calls == ['a', 'b']
    cache.invalidate_id(ids['a'])
    cache.flush()
    assert calls == ['a', 'b', 'a']


def test_measurement_none_return_and_failed_body_cleanup(cache):
    from meltygui import imgui
    @cache.gui(width=64, height=None)
    def body(input_value):
        imgui.dummy(20, 12)
    value = object()
    result = body(value)
    assert result == (False, value)
    node = next(iter(cache.records))
    assert cache.states[node].content_height == 12
    assert cache.graph.rect(node)[3] == 12
    fail = [True]
    @cache.gui(width=64, height=32)
    def failing(input_value):
        if fail[0]:
            raise ValueError('body failed')
        return False, input_value
    previous = imgui.get_current_context()
    with pytest.raises(ValueError, match='body failed'):
        failing(None)
    assert imgui.get_current_context() is previous
    assert cache.stack == []
    fail[0] = False
    body(value)


@pytest.mark.parametrize('option', [{'inject': {'custom': list}}, {'events': ('click',)}])
def test_dependency_injection_options_are_explicitly_rejected(cache, option):
    @cache.gui(width=64, height=32, **option)
    def body(input_value):
        return False, input_value
    with pytest.raises(TypeError, match='injection/events'):
        body(None)
