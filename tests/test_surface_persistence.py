"""Native roots restore their size before a layout can clamp saved dividers."""
from types import SimpleNamespace

import pytest

from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.layout.column_core import _clamp_interior
from meltygui.core.runtime.app_session import AppSession
from meltygui.core.windowing import surface as S
from meltygui.model.tile_model import Split, Tile
from meltygui.state.new_core_model import DrawState


def native_creation_size(monkeypatch, state):
    """Stop at the backend boundary, before allocating an OS window or GL context."""
    class Created(Exception):
        pass

    sizes = []

    def create(width, height, *args):
        sizes.append((width, height))
        raise Created

    monkeypatch.setattr(S, '_capture_defaults', lambda: None)
    monkeypatch.setattr(S, '_unique_title', lambda name: name)
    monkeypatch.setattr(S.titlebar, 'wants_os_decoration', lambda: True)
    monkeypatch.setattr(S.titlebar, 'wants_transparent_framebuffer', lambda: False)
    monkeypatch.setattr(S.glfw_utils, 'apply_opengl_context_hints', lambda: None)
    for name in ('default_window_hints', 'window_hint', 'window_hint_string'):
        monkeypatch.setattr(S.glfw, name, lambda *args: None)
    monkeypatch.setattr(S.glfw, 'create_window', create)
    with pytest.raises(Created):
        S.Surface('Editor', lambda surface: None, width=1280, height=800, state=state)
    return sizes[0]


@pytest.mark.parametrize('legacy', [False, True])
def test_restart_preserves_dividers_before_first_layout(monkeypatch, legacy):
    session = AppSession()
    session.app_state['tiles'] = Split('y', [Tile(), Tile(), Tile()],
                                     [{'y': 470.5078125}, {'y': 1124.62109375}])
    if legacy:
        root = DrawState()
        root.name = 'Editor'
        root.width, root.height = 1410, 1177
        root.window_pos = (0, 30)  # body below the native chrome
        session.draw_state_registry[42] = root
    else:
        state = S.root_surface_state(session, 'app.editor', 'Editor')
        state.size = (1410, 1207)
    restored = loads(dumps(session, excluded=['surface_states'] if legacy else None),
                     run_on_load=False)
    state = S.root_surface_state(restored, 'app.editor', 'Editor')
    width, height = native_creation_size(monkeypatch, state)
    assert (width, height) == (1410, 1207)
    tree = restored.app_state['tiles']
    _clamp_interior([{'y': 60}, *tree.edges, {'y': height}], axis='y')
    assert tree.edges == [{'y': 470.5078125}, {'y': 1124.62109375}]
    # A changed title must not create a new native geometry owner.
    assert S.root_surface_state(restored, 'app.editor', 'file.py') is state


def test_new_window_uses_decorator_size(monkeypatch):
    state = S.root_surface_state(AppSession(), 'app.editor', 'Editor')
    assert native_creation_size(monkeypatch, state) == (1280, 800)


@pytest.mark.parametrize('inset', [0, 24])
def test_observed_size_persists_without_retina_pixels_or_shadow_growth(monkeypatch, inset):
    state = S.SurfaceState()
    surface = object.__new__(S.Surface)
    surface.state, surface.window, surface.chrome = state, object(), bool(inset)
    size = (1410 + 2 * inset, 1207 + 2 * inset)
    monkeypatch.setattr(S.glfw, 'get_window_size', lambda window: size)
    monkeypatch.setattr(S.glfw, 'get_framebuffer_size', lambda window: (size[0] * 2, size[1] * 2))
    monkeypatch.setattr(S.titlebar, 'window_inset', lambda: inset)
    surface.remember_size()
    restored = loads(dumps(state), run_on_load=False)
    assert native_creation_size(monkeypatch, restored) == (1410, 1207)
    size = (0, 0)  # minimized/unmapped windows cannot erase the saved size
    surface.remember_size()
    assert state.size == (1410, 1207)


def test_child_surface_does_not_own_root_size():
    # Children already persist geometry in their own DrawState.
    S.Surface.remember_size(SimpleNamespace(state=None))
