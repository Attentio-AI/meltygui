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
