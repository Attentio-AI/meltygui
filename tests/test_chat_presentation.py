"""Local chat drawing receives its geometry/input and metadata explicitly."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltygui.model.chat_model import project_tint, conversation_tint_setter, _reorder
from meltygui.view import chat_decoration_view


def test_caret_uses_supplied_scale_and_registers_local_action(monkeypatch):
    lines, actions = [], []
    draw_list = SimpleNamespace(add_line=lambda *args: lines.append(args))
    monkeypatch.setattr(chat_decoration_view.imgui, 'get_window_draw_list', lambda: draw_list)
    state = SimpleNamespace(on_action=lambda *args, **kwargs: actions.append((args, kwargs)) or object())
    assert chat_decoration_view._caret(state, 'fold', 10, 20, 40, 30, False, (.3, .5, .7), ui_scale=2)
    assert len(lines) == 2 and all(line[-1] == 3 for line in lines)
    assert actions == [(('left_mouse_clicked',), {'view_id':'fold', 'priority_delta':3,
                                                 'rect':(10, 20, 50, 50)})]


def test_hover_uses_supplied_pointer_without_polling(monkeypatch):
    def unexpected():
        raise AssertionError('helper polled global input')
    monkeypatch.setattr(chat_decoration_view.imgui, 'get_mouse_pos', unexpected)
    assert chat_decoration_view._hovering(10, 20, 30, 40, pointer=(10, 20))
    assert not chat_decoration_view._hovering(10, 20, 30, 40, pointer=(40, 20))


def test_button_restores_cursor_after_draw_failure(monkeypatch):
    positions = []
    monkeypatch.setattr(chat_decoration_view.imgui, 'get_cursor_screen_pos', lambda: (7, 8))
    monkeypatch.setattr(chat_decoration_view.imgui, 'set_cursor_screen_pos', positions.append)
    def fail(*args, **kwargs):
        assert kwargs['height'] == 50
        raise RuntimeError('draw failed')
    monkeypatch.setattr(chat_decoration_view, 'flat_button', fail)
    with pytest.raises(RuntimeError, match='draw failed'):
        chat_decoration_view._button(None, 'button', 'Send', 10, 20, 50, (.2, .3, .4), ui_scale=2)
    assert positions == [(10, 20), (7, 8)]


def test_conversation_values_work_with_plain_supplied_mappings():
    metadata = {'/project': {'tint': (.2, .3, .4)}}
    assert project_tint('/project', metadata) == (.2, .3, .4)
    assert project_tint('/unknown', metadata) is None
    conversation = {}
    write = conversation_tint_setter(conversation)
    write([.4, .5, .6])
    assert conversation == {'tint': (.4, .5, .6)}
    write(None)
    assert conversation == {}
    first, other, second = {'project':'a'}, {'project':'b'}, {'project':'a'}
    chats = {'first':first, 'other':other, 'second':second}
    _reorder(chats, 'first', 1)
    assert list(chats) == ['second', 'other', 'first']
    assert chats['first'] is first and chats['other'] is other


def test_sidebar_resize_reuses_tree_but_content_changes_rebuild(monkeypatch):
    from contextlib import contextmanager
    from meltygui.chat.chat_proxy import Chat
    from meltygui.state.chat_state import ChatInterfaceState
    from meltygui.view import chat_sidebar_view as sidebar

    class Conversations(dict):
        revision = 0

    @contextmanager
    def offscreen_viewport(*args, **kwargs):
        # Exercise real card preparation without painting into a GL window.
        yield 0, 0, (0, 100000, 1000, 100100)

    monkeypatch.setattr(sidebar, '_viewport', offscreen_viewport)
    monkeypatch.setattr(sidebar.imgui, 'get_cursor_screen_pos', lambda: (0, 0))
    monkeypatch.setattr(sidebar, '_text_layout', lambda state, key, text: (text, 20))
    monkeypatch.setattr(sidebar.time, 'time', lambda: 100)
    monkeypatch.setattr(sidebar, 'folder_settings', lambda: {'show_all_folders': False, 'added_folders': []})
    state = ChatInterfaceState()
    state.account = 'test'
    chats = Conversations(chat=Chat({'title': 'First title', 'project': str(Path.home())}))
    chats['chat'].metadata = {}
    sources = [('test', chats, None, '')]
    draw = lambda width: sidebar.draw_chat_sidebar(sources, None, state, width, 500)
    draw(260)
    draw(260)  # Settle any default folder folds discovered during preparation.
    memo = state.viewports['sidebar-cards']['all']
    for width in (470, 180, 430, 260):
        draw(width)
        assert state.viewports['sidebar-cards']['all'] is memo
    chats['chat']['title'] = 'Renamed title'
    chats.revision += 1
    draw(260)
    updated = state.viewports['sidebar-cards']['all']
    assert updated is not memo
    assert any(child[5] == 'Renamed title' for card in updated[1] for child in card[6])


def test_sidebar_project_link_filters_both_panes_and_unlinks(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from meltygui.chat.chat_proxy import Chat
    from meltygui.state.chat_state import ChatInterfaceState
    from meltygui.view import chat_sidebar_view as sidebar

    class Conversations(dict):
        revision = 0

    @contextmanager
    def offscreen_viewport(*args, **kwargs):
        # Exercise real card preparation without painting into a GL window.
        yield 0, 0, (0, 100000, 1000, 100100)

    monkeypatch.setattr(sidebar, '_viewport', offscreen_viewport)
    monkeypatch.setattr(sidebar.imgui, 'get_cursor_screen_pos', lambda: (0, 0))
    monkeypatch.setattr(sidebar, '_text_layout', lambda state, key, text: (text, 20))
    monkeypatch.setattr(sidebar.time, 'time', lambda: 100)
    monkeypatch.setattr(sidebar, 'folder_settings', lambda: {'show_all_folders': False, 'added_folders': []})
    state = ChatInterfaceState()
    state.account = 'test'
    from meltygui.model import chat_folder_model
    monkeypatch.setattr(chat_folder_model, 'folder_settings', lambda: {'show_all_folders': False, 'added_folders': [str(tmp_path / 'outside')]})
    monkeypatch.setattr(sidebar, 'empty_recent_chat', lambda chat: False)
    root = tmp_path / 'project'
    paths = [root, root / 'child', tmp_path / 'project-other']
    chats = Conversations({str(i): Chat({'title': str(i), 'project': str(path)}) for i, path in enumerate(paths)})
    for chat in chats.values():
        chat.metadata = {}
    sources = [('test', chats, None, '')]
    for pane in ('all', 'recent'):
        for selected, expected in ((str(root), {'0', '1'}), (str(paths[2]), {'2'}), (None, {'0', '1', '2'})):
            sidebar.draw_chat_sidebar(sources, None, state, 260, 500, pane=pane, project_filter=selected)
            cards = state.viewports['sidebar-cards'][pane][1]
            assert {row[4] for card in cards for row in card[5]} == expected
            if selected:
                assert all(Path(card[1]).is_relative_to(selected) for card in cards)


def test_decoration_definition_hotswaps_in_place():
    from test_render_func_integration import _init_melty
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    _init_melty()
    source = Path(chat_decoration_view.__file__).read_text()
    original = chat_decoration_view._color
    original_value = original((.2, .3, .4))
    stamp_module_baseline(chat_decoration_view, source)
    edited = source.replace('return pack_color(*tint[:3], alpha)', 'return pack_color(*tint[:3], alpha * 0.5)')
    try:
        assert _recompile_module(chat_decoration_view, edited, chat_decoration_view.__file__) is None
        assert chat_decoration_view._color is original
        assert original((.2, .3, .4)) != original_value
    finally:
        assert _recompile_module(chat_decoration_view, source, chat_decoration_view.__file__) is None


def test_navigation_overlay_follows_live_bounds_without_mutating_scroll(monkeypatch):
    from unittest.mock import Mock
    from meltygui.state.chat_state import ChatNavigationState
    from meltygui.view import chat_view as sidebar
    monkeypatch.setattr(sidebar.Melty, 'ui_scale', 1)
    monkeypatch.setattr(sidebar.Melty, 'font_mgr', None)
    monkeypatch.setattr(sidebar.imgui, 'get_text_line_height', lambda: 20)
    monkeypatch.setattr(sidebar, '_text_tint', lambda tint: tint)
    monkeypatch.setattr(sidebar, '_color', lambda *args: 1)
    state = ChatNavigationState()
    # First pane is fixed-height; the last stretches with the sidebar's bottom.
    state.scrollbars = [(30, 100, 400, 100, 4, None), (160, 200, 800, 300, 4, 400)]
    state.show_all = (0, 4, 90, 26, True)
    view = SimpleNamespace(abs_left=10, abs_top=20, width=260, height=400,
                           current_tint=(.2, .3, .4), misc={'chat_navigation_state': state})
    before = Mock()
    sidebar.draw_chat_navigation_overlay(view, before)
    first = before.add_rect_filled.call_args_list[0].args
    last = before.add_rect_filled.call_args_list[1].args
    assert first[:4] == (259, 75, 266, 100)
    assert last[:4] == (259, 255, 266, 305)
    view.width, view.height = 400, 500
    after = Mock()
    sidebar.draw_chat_navigation_overlay(view, after)
    first = after.add_rect_filled.call_args_list[0].args
    last = after.add_rect_filled.call_args_list[1].args
    assert first[:4] == (399, 75, 406, 100)
    assert last[:4] == (399, 292.5, 406, 405)
    assert after.add_text.call_args.args[0] - before.add_text.call_args.args[0] == 140
    assert state.scrollbars[1][3] == 300
    # A second view's state is independent and has nothing to paint yet.
    view.misc = {'chat_navigation_state': ChatNavigationState()}
    empty = Mock()
    sidebar.draw_chat_navigation_overlay(view, empty)
    assert not empty.mock_calls


def test_navigation_overlay_places_account_child_before_scrollbar_replay(monkeypatch):
    from unittest.mock import Mock
    from meltygui.core.rendering import overlay
    from meltygui.state.chat_state import ChatNavigationState
    from meltygui.view import chat_view as sidebar
    monkeypatch.setattr(sidebar, '_text_tint', lambda tint: tint)
    monkeypatch.setattr(sidebar, '_color', lambda *args: 1)
    place, paint = Mock(), Mock()
    monkeypatch.setattr(overlay, 'place_overlay_view', place)
    monkeypatch.setattr(overlay, 'paint_cached_view', paint)
    state = ChatNavigationState()
    state.accounts_view = object()
    state.accounts_rect = (4, 30, 6, 200)
    view = SimpleNamespace(abs_left=10, abs_top=20, width=400, height=180,
                           current_tint=(.2, .3, .4), abs_clip_rect=(10, 20, 410, 200),
                           _blit_served_frame=sidebar.Melty.frame_count,
                           misc={'chat_navigation_state': state})
    draw_list = Mock()
    sidebar.draw_chat_navigation_overlay(view, draw_list)
    place.assert_called_once_with(state.accounts_view, (14, 50, 390, 150), view.abs_clip_rect)
    paint.assert_called_once_with(state.accounts_view)


def test_navigation_overlay_hotswaps_without_replacing_prepared_state(monkeypatch):
    from unittest.mock import Mock
    from test_render_func_integration import _init_melty
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    from meltygui.state.chat_state import ChatNavigationState
    from meltygui.view import chat_view
    _init_melty()
    monkeypatch.setattr(chat_view.Melty, 'ui_scale', 1)
    state = ChatNavigationState()
    state.scrollbars = [(30, 100, 400, 100, 4, None)]
    view = SimpleNamespace(abs_left=10, abs_top=20, width=260, height=400,
                           current_tint=(.2, .3, .4), misc={'chat_navigation_state': state})
    original = chat_view.draw_chat_navigation_overlay
    source = Path(chat_view.__file__).read_text()
    stamp_module_baseline(chat_view, source)
    edited = source.replace('bar_width, minimum_thumb = Melty.px(7), Melty.px(24)',
                            'bar_width, minimum_thumb = Melty.px(9), Melty.px(24)')
    assert edited != source
    try:
        assert _recompile_module(chat_view, edited, chat_view.__file__) is None
        assert chat_view.draw_chat_navigation_overlay is original
        drawing = Mock()
        original(view, drawing)
        assert drawing.add_rect_filled.call_args.args[:4] == (257, 75, 266, 100)
        assert view.misc['chat_navigation_state'] is state
    finally:
        assert _recompile_module(chat_view, source, chat_view.__file__) is None
