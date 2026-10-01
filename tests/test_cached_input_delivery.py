"""Nested context clicks survive an ancestor's cached press frame."""
from types import SimpleNamespace
from unittest.mock import Mock

from meltygui.core.cache.tile_cache import TileCacheMasked, _Ctx
from meltygui.core.input.input_handler import InputHandler


def context(view, cached=False):
    return _Ctx(view, 'parent', (0, 0), (300, 200), 0, 0, cached, False)


def test_cached_parent_keeps_nested_click_target():
    handler = InputHandler()
    child = SimpleNamespace(replay_body_actions=lambda: handler.register_hovered(
        'text', ['right_mouse_clicked'], priority=0, tile_id='text-tile'))
    parent = SimpleNamespace(replay_body_actions=Mock())
    ancestor = context(None)
    cache = SimpleNamespace(enabled=True, _stack=[ancestor], _tiles={})
    cache.retain_input_view = lambda ds: TileCacheMasked.retain_input_view(cache, ds)
    capture = context(parent)
    cache._stack = [capture]
    cache.retain_input_view(child)
    cache._tiles['parent'] = SimpleNamespace(input_views=capture.input_views)
    cache._stack = [ancestor]

    def frame(cached):
        handler.begin_frame()
        handler.register_hovered('parent', ['right_mouse_clicked'], priority=1, tile_id='parent-tile')
        if cached:
            TileCacheMasked.finish_cached_input(cache, context(parent, True))
        else:
            child.replay_body_actions()

    frame(False)
    handler.feed_down('right_mouse', 10, 10)
    handler.process_frame()
    frame(True)  # holding the button lets the enclosing body use its snapshot
    handler.feed_up('right_mouse', 10, 10)
    result = handler.process_frame()[0]
    assert 'parent' not in result
    assert result['text']['right_mouse_clicked'].tile_id == 'text-tile'
    assert ancestor.input_views == (child, parent)


def test_new_capture_drops_hidden_child_input():
    old_child = SimpleNamespace(replay_body_actions=Mock())
    new_child = SimpleNamespace(replay_body_actions=Mock())
    parent = SimpleNamespace(replay_body_actions=Mock())
    tile = SimpleNamespace(input_views=(old_child,))
    cache = SimpleNamespace(enabled=True, _stack=[], _tiles={'parent': tile})
    cache.retain_input_view = lambda ds: TileCacheMasked.retain_input_view(cache, ds)
    capture = context(parent)
    cache._stack = [capture]
    cache.retain_input_view(new_child)
    tile.input_views = capture.input_views
    cache._stack = []
    TileCacheMasked.finish_cached_input(cache, context(parent, True))
    old_child.replay_body_actions.assert_not_called()
    new_child.replay_body_actions.assert_called_once()


def test_pass_through_deliveries_own_separate_tile_ids():
    handler = InputHandler()
    handler.register_hovered('child', ['non_blocking_right_mouse_clicked'], 0, tile_id='child-tile')
    handler.register_hovered('parent', ['right_mouse_clicked'], 1, tile_id='parent-tile')
    handler.feed_down('right_mouse', 10, 10)
    handler.feed_up('right_mouse', 10, 10)
    events = handler.process_frame()[0]
    child = events['child']['non_blocking_right_mouse_clicked']
    parent = events['parent']['right_mouse_clicked']
    assert child.tile_id == 'child-tile'
    assert parent.tile_id == 'parent-tile'
    assert child is not parent


def test_hover_keeps_non_invalidating_sentinel():
    handler = InputHandler()
    handler.register_hovered('text', ['cursor_hover'], tile_id='text-tile')
    event = handler.process_frame()[0]['text']['cursor_hover']
    assert event.tile_id == 'hovered'


def test_click_invalidates_ancestors_even_after_hover(monkeypatch):
    from meltygui.core import melty
    from meltygui.core.melty import Melty
    from meltygui.core.input.input_handler import InputEvent
    invalidate = Mock()
    monkeypatch.setattr(Melty, 'cache', SimpleNamespace(invalidate_up=invalidate))
    monkeypatch.setattr(Melty, 'on_scroll', False)
    monkeypatch.setattr(Melty, 'space_mouse_drag', False)
    monkeypatch.setattr(melty.imgui, 'is_mouse_down', lambda button: False)
    monkeypatch.setattr(Melty, 'events', {'text': {
        'cursor_hover': InputEvent('cursor', None, 'hovered'),
        'right_mouse_clicked': InputEvent('right_mouse', 'clicked', 'text-tile'),
        'right_mouse_up': InputEvent('right_mouse', 'up', 'text-tile'),
    }})
    Melty.invalidate_event_targets()
    invalidate.assert_called_once_with('text-tile', max_depth=10, force=True)
    invalidate.reset_mock()
    Melty.events['text'] = {'cursor_hover': InputEvent('cursor', None, 'hovered')}
    Melty.invalidate_event_targets()
    invalidate.assert_not_called()
