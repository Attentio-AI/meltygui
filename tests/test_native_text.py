"""Committed Unicode reaches the cached editor without desktop key synthesis."""
from meltygui.editor.text_editor import typed_characters
from meltygui.core.melty import Melty
from meltygui.core.windowing import window_constants as keys


def test_unicode_service_owns_printable_text_and_normalizes_newlines():
    assert typed_characters([(keys.KEY_A, 0)], ['café ', '🙂', '\r\nx\ry']) == 'café 🙂\nx\ny'
    assert typed_characters([(keys.KEY_A, 0)], []) == ''


def test_desktop_mapping_keeps_shift_and_control_behavior():
    assert typed_characters([(keys.KEY_A, 0), (keys.KEY_B, keys.MOD_SHIFT),
                             (keys.KEY_C, keys.MOD_CONTROL)]) == 'aB'


def test_committed_text_forces_the_focused_editor_to_consume_its_events(monkeypatch):
    monkeypatch.setattr(Melty, 'frame_text_events', ['α'])
    monkeypatch.setattr(Melty, 'frame_key_events', [])
    assert Melty.focused_key_pending()
