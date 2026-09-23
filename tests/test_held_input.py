"""Mouse drags keep cooperative parsing deferred without reading every key."""
from types import SimpleNamespace

import pytest

from meltygui.core.input import pynput_backend
from meltygui.core.input.pynput_backend import GlfwQueueBackend


@pytest.mark.parametrize('button', range(5))
def test_mouse_hold_does_not_materialize_keyboard_state(monkeypatch, button):
    class MouseOnly:
        key_ctrl = key_shift = key_alt = key_super = False
        @property
        def keys_down(self):
            raise AssertionError('mouse hold already establishes activity')

    monkeypatch.setattr(pynput_backend.imgui, 'is_mouse_down', lambda i: i == button)
    assert GlfwQueueBackend._any_input_held(MouseOnly())


@pytest.mark.parametrize('held', [False, True])
def test_keyboard_only_and_idle_input_still_work(monkeypatch, held):
    monkeypatch.setattr(pynput_backend.imgui, 'is_mouse_down', lambda i: False)
    io = SimpleNamespace(key_ctrl=False, key_shift=False, key_alt=False,
                         key_super=False, keys_down=[False, held, False])
    assert GlfwQueueBackend._any_input_held(io) is held
