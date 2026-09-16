"""The process-wide host pump must defer during a child window's drag."""
from types import SimpleNamespace

import pytest
import meltygui_imgui as imgui

from meltygui.core.render_host import RenderHost
from meltygui.core.melty import Melty
from meltygui.core.surface import Surface
import meltygui.core.window_api as glfw


@pytest.fixture
def host_pump(monkeypatch):
    calls = []
    host = SimpleNamespace(draw_needed=lambda: True, draw=lambda: calls.append('draw'))
    monkeypatch.setattr(RenderHost, 'all', classmethod(lambda cls: [host]))
    monkeypatch.setattr(RenderHost, 'typing_hold', classmethod(lambda cls: False))
    monkeypatch.setattr(Melty, 'on_scroll', False)
    monkeypatch.setattr(Melty, 'space_mouse_drag', False)
    monkeypatch.setattr(imgui, 'is_mouse_down', lambda button: False)
    monkeypatch.setattr(Surface, 'all', [])
    return calls


@pytest.mark.parametrize('held_button', [glfw.MOUSE_BUTTON_LEFT, glfw.MOUSE_BUTTON_RIGHT,
                                        glfw.MOUSE_BUTTON_MIDDLE])
def test_child_drag_defers_parent_host_pump_then_resumes(monkeypatch, host_pump, held_button):
    parent, child = object(), object()
    monkeypatch.setattr(Surface, 'all', [SimpleNamespace(window=parent, closed=False),
                                       SimpleNamespace(window=child, closed=False)])
    held = True
    monkeypatch.setattr(glfw, 'get_mouse_button',
                        lambda window, button: glfw.PRESS if held and window is child
                        and button == held_button else glfw.RELEASE)
    RenderHost.draw_all()
    assert host_pump == []
    held = False
    RenderHost.draw_all()
    assert host_pump == ['draw']


def test_destroyed_and_closed_surfaces_do_not_hold_host_pump(monkeypatch, host_pump):
    monkeypatch.setattr(Surface, 'all', [SimpleNamespace(window=None, closed=False),
                                       SimpleNamespace(window=object(), closed=True)])
    monkeypatch.setattr(glfw, 'get_mouse_button',
                        lambda *_: pytest.fail('polled a closed surface'))
    RenderHost.draw_all()
    assert host_pump == ['draw']


def test_single_context_drag_still_defers_hosts(monkeypatch, host_pump):
    monkeypatch.setattr(imgui, 'is_mouse_down', lambda button: button == 1)
    RenderHost.draw_all()
    assert host_pump == []
