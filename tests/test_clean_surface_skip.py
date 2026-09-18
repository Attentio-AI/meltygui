"""An OS window nothing asked to redraw skips its frame (Surface.wants_frame):
render requests belong to the window whose frame or GLFW callback made them,
unattributable ones and reported edits to every window, by-object
invalidations reach every window's cache, and a skipped parent's child
windows stay open."""
import threading
from types import SimpleNamespace

import pytest

import meltygui.core.windowing.glfw_utils as glfw_utils
from meltygui.core.cache.tile_cache import TileCacheMasked
from meltygui.core.melty import Melty
from meltygui.core.runtime import app


class Window:
    """Stands in for a Surface: the request bookkeeping only needs identity."""


@pytest.fixture
def windows(monkeypatch):
    first, second = Window(), Window()
    monkeypatch.setattr(Melty, 'glfw_window', object())
    monkeypatch.setattr(glfw_utils, 'render_scope', None)
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(glfw_utils, '_open_surfaces', {first, second})
    monkeypatch.setattr(glfw_utils, '_requested_surfaces', set())
    monkeypatch.setattr(glfw_utils, '_all_surfaces_generation', 0)
    monkeypatch.setattr(glfw_utils.glfw, 'post_empty_event', lambda: None, raising=False)
    glfw_utils._needs_render.clear()
    return first, second


def _requested(surface, generation=0):
    return glfw_utils.take_surface_request(surface, generation)[0]


def test_request_inside_a_window_scope_is_that_windows(windows, monkeypatch):
    first, second = windows
    monkeypatch.setattr(glfw_utils, 'render_scope', first)
    glfw_utils.request_render()
    assert glfw_utils._needs_render.is_set()
    assert _requested(first) and not _requested(second)
    assert not _requested(first)                       # consumed


def test_unattributed_request_is_every_windows(windows):
    first, second = windows
    glfw_utils.request_render()
    assert _requested(first) and _requested(second)


def test_worker_thread_request_ignores_the_render_threads_scope(windows, monkeypatch):
    first, second = windows
    monkeypatch.setattr(glfw_utils, 'render_scope', first)
    worker = threading.Thread(target=glfw_utils.request_render)
    worker.start()
    worker.join()
    assert _requested(second)


def test_reported_change_draws_and_wakes_every_window(windows, monkeypatch):
    first, second = windows
    monkeypatch.setattr(glfw_utils, 'render_scope', first)
    glfw_utils.note_shared_change()
    assert glfw_utils._needs_render.is_set()
    requested, generation = glfw_utils.take_surface_request(second, 0)
    assert requested
    assert not glfw_utils.take_surface_request(second, generation)[0]


def test_request_for_another_window_restores_the_scope(windows, monkeypatch):
    first, second = windows
    monkeypatch.setattr(glfw_utils, 'render_scope', first)
    glfw_utils.request_surface_render(second)
    assert glfw_utils.render_scope is first
    assert _requested(second) and not _requested(first)


def test_skipped_parent_keeps_its_child_window(monkeypatch):
    monkeypatch.setattr(Melty, 'app_tick', 11)
    child = SimpleNamespace(closed=False, stale=False)
    skipped = SimpleNamespace(tick=10, surface=child, parent_surface=SimpleNamespace(drawn_tick=10))
    monkeypatch.setattr(Melty, 'surface_windows', {'picker': skipped})
    app._close_stale_children()
    assert child.closed is False
    skipped.parent_surface.drawn_tick = 11             # the parent drew and made no call
    app._close_stale_children()
    assert child.closed is True and child.stale is True


def test_child_request_differs_by_identity_and_plain_equality():
    value = {'rows': [1, 2]}
    callback = lambda: None
    req = SimpleNamespace(passed_value=value, kwargs={'name': 'picker', 'on_pick': callback, 'size': (3, 4)})
    same = {'name': 'picker', 'on_pick': callback, 'size': (3, 4)}
    assert not Melty.surface_request_differs(req, value, same)
    assert Melty.surface_request_differs(req, {'rows': [1, 2]}, same)          # another object
    assert Melty.surface_request_differs(req, value, same | {'size': (3, 5)})
    assert Melty.surface_request_differs(req, value, same | {'on_pick': lambda: None})
    assert Melty.surface_request_differs(req, value, {'name': 'picker'})


def test_late_echo_of_the_childs_edit_keeps_its_latest_value(monkeypatch):
    """The child window draws without its parent, from its stored call. The
    parent handing back an edit ticks later must not undo what was typed
    since; a value of the parent's own replaces the child's."""
    from meltygui.state.new_core_model import DrawState
    monkeypatch.setattr(Melty, 'surface_windows', {})
    monkeypatch.setattr(Melty, 'surface_requests', [])
    monkeypatch.setattr(glfw_utils, 'request_surface_render', lambda surface: asked.append(surface))
    asked = []
    draw_state, kwargs = DrawState(), {'window_size': (520, 300)}
    req = Melty.surface_window_request('log', 'log', '', kwargs, draw_state)
    req.surface = child = SimpleNamespace(closed=False)
    for typed in ('c', 'ch', 'chi'):                   # three child frames, no parent frame
        req.input_value = typed
        req.unechoed.append(typed)
    Melty.surface_window_request('log', 'log', 'c', kwargs, draw_state)      # the parent catches up
    assert req.input_value == 'chi' and asked == []
    Melty.surface_window_request('log', 'log', 'chi', kwargs, draw_state)
    assert req.input_value == 'chi' and req.unechoed == [] and asked == []
    Melty.surface_window_request('log', 'log', 'cleared by the parent', kwargs, draw_state)
    assert req.input_value == 'cleared by the parent' and asked == [child]


def test_object_invalidation_reaches_the_other_windows_cache(monkeypatch):
    drawing, other, unrelated = (TileCacheMasked() for _ in range(3))
    asked = []
    monkeypatch.setattr(TileCacheMasked, 'window_caches', {
        drawing: lambda: asked.append('drawing'), other: lambda: asked.append('other'),
        unrelated: lambda: asked.append('unrelated')})
    model = SimpleNamespace()
    other.py_id_to_keys[f'{id(model)}.text'] = {'view_key'}
    invalidated = []
    monkeypatch.setattr(other, 'invalidate', lambda key, **kwargs: invalidated.append(key))
    drawing.invalidate_by_obj(model, 'text')
    assert invalidated == ['view_key']
    assert asked == ['other']
