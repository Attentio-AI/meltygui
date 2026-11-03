"""Client decorations follow the live compositor without application-specific buttons."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from meltygui import imgui
from meltygui.core.windowing import titlebar_buttons as buttons, titlebar


def manifest(side='right', icon='/tmp/custom-symbolic.svg', action='custom-helper'):
    return json.dumps({'version': 1, 'side': side, 'buttons': [
        dict(id=0, icon='×', alt_icon='', action='close-helper', state='', role='close'),
        dict(id=1, icon=icon, alt_icon='', action=action, state='', role=''),
    ]})


def test_visual_order_and_changed_actions_have_distinct_identity():
    layout, items = buttons.parse_hyprbars(manifest())
    assert not layout.left
    assert [items[key]['role'] for key in layout.right] == ['', 'close']
    left, _ = buttons.parse_hyprbars(manifest('left'))
    assert left.left == tuple(reversed(layout.right))
    changed, _ = buttons.parse_hyprbars(manifest(action='different-helper'))
    assert layout.right[0] != changed.right[0]
    assert layout.right[1] == changed.right[1]


@pytest.mark.parametrize('reply', ['unknown request', '{}', '{"version":2,"side":"right"}',
                                   '{"version":1,"side":"right","buttons":[{}]}'])
def test_invalid_manifests_fall_back(reply):
    with pytest.raises((ValueError, KeyError, TypeError)):
        buttons.parse_hyprbars(reply)


def test_file_icon_uses_square_metrics_and_standard_close_is_preserved(monkeypatch):
    layout, items = buttons.parse_hyprbars(manifest())
    monkeypatch.setitem(buttons._state, 'buttons', items)
    monkeypatch.setattr(titlebar, 'control_kinds', lambda: layout)
    close = Mock()
    monkeypatch.setattr(titlebar, '_close_window', close)
    monkeypatch.setattr(titlebar, '_close_blocked_by_merge', lambda: False)
    imgui.new_frame()
    rendered = titlebar._button_layout(800, maximized=False)
    assert rendered[0][1] == '/tmp/custom-symbolic.svg'
    assert 0 < rendered[0][2][2] - rendered[0][2][0] < 100
    titlebar._activate_button(layout.right[1], 'window')
    close.assert_called_once_with('window')
    titlebar._activate_button('system:removed', 'window')
    close.assert_called_once()


def test_custom_action_has_explicit_target_and_never_forks_or_changes_focus(monkeypatch):
    layout, items = buttons.parse_hyprbars(manifest(action='generic-helper --flag'))
    monkeypatch.setitem(buttons._state, 'buttons', items)
    monkeypatch.setattr(buttons.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=target))
    run = Mock()
    monkeypatch.setattr(buttons.subprocess, 'run', run)
    assert buttons.activate_system_button(layout.right[0], '0xab12')
    args, kwargs = run.call_args
    assert args[0] == ['/bin/sh', '-c', 'generic-helper --flag']
    assert kwargs['env']['HYPRLAND_WINDOW_ADDRESS'] == '0xab12'
    assert kwargs['close_fds'] is False
    assert not buttons.activate_system_button(layout.right[0], '0x12; bad')
    assert run.call_count == 1


def test_artwork_is_decoded_only_when_file_changes(tmp_path, monkeypatch):
    from meltygui.model import folder_icon_model
    icon = tmp_path / 'custom-symbolic.svg'
    icon.write_text('<svg/>')
    decode = Mock(return_value=folder_icon_model.IconPixels(icon, 1, 1, bytes([0, 0, 0, 120])))
    monkeypatch.setattr(folder_icon_model, 'decode_icon', decode)
    _, items = buttons.parse_hyprbars(manifest(icon=str(icon)))
    first = buttons._decode_artwork(items, {})
    assert first[str(icon)][1].data == bytes([255, 255, 255, 120])
    assert buttons._decode_artwork(items, first)[str(icon)] is first[str(icon)]
    decode.assert_called_once()
    icon.write_text('<svg changed="yes"/>')
    buttons._decode_artwork(items, first)
    assert decode.call_count == 2


def test_client_controls_announce_once_and_reassert_after_probe(monkeypatch):
    from meltygui.core.windowing import geometry_feed
    monkeypatch.setitem(buttons._state, 'source', 'hyprbars')
    monkeypatch.setitem(buttons._state, 'client_controls', {})
    monkeypatch.setitem(buttons._state, 'probed_at', 1)
    monkeypatch.setattr(buttons.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=target))
    request = Mock()
    monkeypatch.setattr(geometry_feed, 'hypr_request', request)
    buttons.sync_client_controls('0xab12', True)
    buttons.sync_client_controls('0xab12', True)
    assert request.call_count == 1
    assert 'hl.get_window("address:0xab12")' in request.call_args.args[0]
    assert 'tag="+melty-client-controls"' in request.call_args.args[0]
    buttons.sync_client_controls('0xab12', False)
    assert request.call_count == 2
    assert 'tag="-melty-client-controls"' in request.call_args.args[0]
    monkeypatch.setitem(buttons._state, 'probed_at', 2)
    buttons.sync_client_controls('0xab12', False)
    assert request.call_count == 3
    buttons.sync_client_controls(None, True)
    assert request.call_count == 3


def test_startup_csd_rule_is_registered_without_needing_a_mapped_window(monkeypatch):
    from meltygui.core.windowing import geometry_feed
    monkeypatch.setattr(buttons.sys, 'platform', 'linux')
    request = Mock()
    monkeypatch.setattr(geometry_feed, 'hypr_request', request)
    buttons.prepare_client_decorations('custom.project+app')
    command = request.call_args.args[0]
    assert 'hl.window_rule' in command
    assert '["hyprbars:force_bar"]=false' in command
    assert 'get_window' not in command
    assert json.dumps(r'custom\.project\+app') in command
