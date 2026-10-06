"""Debounced coloring wakes once when ready and cannot retain closed views."""
import gc
import weakref

import pytest

from meltygui.core.cache import deferred_invalidation as deferred
from meltygui.core.melty import Melty
from meltygui.core.windowing import glfw_utils


class View:
    def __init__(self):
        self.closed = False
        self._tile_id = 'editor'
        self.misc = {}
        self.misc_used = set()


class Cache:
    def __init__(self, view):
        self.key_to_draw_state = {view._tile_id: view}
        self.invalidations = []

    def invalidate_up(self, key, **kwargs):
        self.invalidations.append(key)


@pytest.fixture
def clock(monkeypatch):
    now, timers, posts, wakes = [10.0], [], [], []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback, self.cancelled = delay, callback, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(deferred.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(deferred.threading, 'Timer', Timer)
    monkeypatch.setattr(Melty, 'post_to_render', posts.append)
    monkeypatch.setattr(glfw_utils, 'request_render', lambda: wakes.append(True))
    return now, timers, posts, wakes


def test_many_draws_wait_for_one_refresh_and_keep_the_original_cache(clock, monkeypatch):
    now, timers, posts, wakes = clock
    view = View()
    cache = Cache(view)
    refresh = deferred.DeferredInvalidation()
    for _ in range(100):
        refresh.schedule(view, cache, 10.25)
    assert len(timers) == 1
    assert not posts and not wakes and not cache.invalidations
    # Another OS window may be current by the time the result is ready.
    other = Cache(View())
    monkeypatch.setattr(Melty, 'cache', other)
    now[0] = 10.25
    timers[0].callback()
    assert len(posts) == 1 and not cache.invalidations
    posts.pop()()
    assert cache.invalidations == ['editor']
    assert not other.invalidations
    assert len(wakes) == 1
    assert refresh._token is None


def test_typing_extends_deadline_without_a_timer_or_frame_per_input(clock):
    now, timers, posts, wakes = clock
    view = View()
    cache = Cache(view)
    refresh = deferred.DeferredInvalidation()
    refresh.schedule(view, cache, 10.25)
    for i in range(100):
        refresh.schedule(view, cache, 10.25 + i / 100)
    assert len(timers) == 1
    now[0] = 10.25
    timers[0].callback()
    assert len(timers) == 2 and not posts and not wakes
    now[0] = 11.24
    timers[1].callback()
    # An input arriving between the timer and render delivery also postpones.
    refresh.schedule(view, cache, 11.5)
    posts.pop()()
    assert not cache.invalidations
    now[0] = 11.5
    timers[-1].callback()
    posts.pop()()
    assert cache.invalidations == ['editor']


@pytest.mark.parametrize('after_post', [False, True])
def test_cancel_discards_even_an_already_posted_refresh(clock, after_post):
    now, timers, posts, wakes = clock
    view = View()
    cache = Cache(view)
    refresh = deferred.DeferredInvalidation()
    refresh.schedule(view, cache, 10.25)
    now[0] = 10.25
    if after_post:
        timers[0].callback()
    refresh.cancel()
    timers[0].callback()
    for callback in posts:
        callback()
    assert not cache.invalidations and not wakes


@pytest.mark.parametrize('discard', ['close', 'replace', 'collect'])
def test_closed_or_replaced_view_is_never_invalidated_or_retained(clock, discard):
    now, timers, posts, wakes = clock
    view = View()
    cache = Cache(view)
    refresh = deferred.DeferredInvalidation()
    refresh.schedule(view, cache, 10.25)
    if discard == 'close':
        view.closed = True
    elif discard == 'replace':
        cache.key_to_draw_state['editor'] = View()
    else:
        held = weakref.ref(view)
        cache.key_to_draw_state.clear()
        del view
        gc.collect()
        assert held() is None
    now[0] = 10.25
    timers[0].callback()
    for callback in posts:
        callback()
    assert not cache.invalidations and not wakes


@pytest.mark.parametrize('kind', ['tints', 'usages'])
def test_symbol_collectors_do_not_poll_frames_during_debounce(clock, monkeypatch, kind):
    from meltygui.editor import text_editor as editor
    now, timers, posts, wakes = clock
    view = View()
    monkeypatch.setattr(Melty, 'cache', Cache(view))
    monkeypatch.setattr(Melty, '_last_input_time', 0.0)
    monkeypatch.setattr(editor, 'request_render', lambda: pytest.fail('Busy-loop frame request'))
    monkeypatch.setattr(editor, '_anchor_span_set', lambda *args: None)
    collected = []
    if kind == 'tints':
        value = ((), (), (), {})
        monkeypatch.setattr(editor, '_collect_def_tints', lambda *args: collected.append(args) or value)
        monkeypatch.setattr(editor, '_resolve_def_tints', lambda fresh, *args: fresh)
        collect = editor._def_tints
    else:
        value = ()
        monkeypatch.setattr(editor, '_collect_usage_spans', lambda *args: collected.append(args) or value)
        monkeypatch.setattr(editor, '_resolve_usage_spans', lambda fresh, *args: fresh)
        collect = editor._usage_spans
    first, changed = {}, {}
    assert collect(view, 'text', first) == value
    now[0] = 10.1
    for _ in range(100):
        assert collect(view, 'text', changed) == value
    assert len(collected) == 1 and len(timers) == 1
    assert not posts and not wakes
    now[0] = 10.25
    timers[0].callback()
    posts.pop()()
    collect(view, 'text', changed)
    assert len(collected) == 2
    assert Melty.cache.invalidations == ['editor']


def test_default_symbol_placeholder_is_not_an_inflight_parse(clock, monkeypatch):
    from meltygui.editor import text_editor as editor
    now, timers, posts, wakes = clock
    view = View()
    monkeypatch.setattr(Melty, '_last_input_time', 0.0)
    monkeypatch.setattr(editor, '_anchor_span_set', lambda *args: None)
    monkeypatch.setattr(editor, '_resolve_def_tints', lambda fresh, *args: fresh)
    collected = []
    monkeypatch.setattr(editor, '_collect_def_tints',
                        lambda *args: collected.append(args) or ((), (), (), {}))

    class Tree(dict):
        symbol_usage = [None]

    editor._def_tints(view, 'text', Tree())
    now[0] += 1
    pending = Tree()
    editor._def_tints(view, 'text', pending)
    assert len(collected) == 2 and not timers
    pending = Tree()
    pending._needs_distribute = True
    pending.symbol_usage = {'carried': True}
    now[0] += 1
    editor._def_tints(view, 'text', pending)
    assert len(collected) == 2 and not timers
