"""Window annotations own @gui dispatch and lifetime, not application code."""
from types import SimpleNamespace
import pytest

from meltygui.core.rendering.gui_prototype import gui, _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow
from meltygui.core.conversion.dict_conversion import DictConversion


def host(monkeypatch):
    from meltygui.core.melty import Melty
    monkeypatch.setattr(Melty, 'root_fill', (400., 250., 30.), raising=False)
    window = _GuiWindow(graphics=False)
    monkeypatch.setattr(window.cache, 'process_host_input', lambda **kwargs: None)
    monkeypatch.setattr(window.cache, 'present', lambda origin: None)
    surface = SimpleNamespace(_gui_prototype=window, request_frame=lambda: None)
    return window, surface


def test_public_gui_requires_window_scope():
    @gui
    def view(input_value: object):
        return False, input_value

    with pytest.raises(RuntimeError, match='@glfw_window'):
        view(None)


def test_window_dispatch_caches_and_forwards_dynamic_inputs(monkeypatch):
    from meltygui.core.runtime.app import _root_body
    window, surface = host(monkeypatch)
    calls = []

    @gui
    def view(input_value: object, draw_state=None, new_argument=3, **kwargs):
        calls.append((new_argument, draw_state.width, draw_state.height))
        return False, input_value

    config = {'view_kwargs': {'new_argument': 8}}
    body = _root_body(view, 'native root', config=config)
    body(surface)
    body(surface)
    assert calls == [(8, 400., 250.)]
    config['view_kwargs']['new_argument'] = 9
    body(surface)
    assert calls[-1] == (9, 400., 250.) and len(calls) == 2
    window.cache.invalidate(view)
    body(surface)
    assert len(calls) == 3
    assert _window.get() is None
    window.close()


def test_two_windows_own_separate_injected_state_and_close_releases_it(monkeypatch):
    import weakref
    import gc

    class LocalState(DictConversion):
        def __init__(self):
            super().__init__()
            self.count = 0

    states = []

    @gui(use_cache=False)
    def app(input_value: object, state: LocalState = None):
        state.count += 1
        states.append(weakref.ref(state))
        return False, state.count

    first, left = host(monkeypatch)
    second, right = host(monkeypatch)
    assert first.draw(left, app, {}) == (False, 1)
    assert first.draw(left, app, {}) == (False, 2)
    assert second.draw(right, app, {}) == (False, 1)
    assert states[0]() is states[1]() and states[0]() is not states[2]()
    first.close()
    gc.collect()
    assert states[0]() is None and states[2]() is not None
    second.close()
    assert _window.get() is None


def test_window_scope_unwinds_on_error_and_survives_replaced_body(monkeypatch):
    window, surface = host(monkeypatch)

    @gui(use_cache=False)
    def app(input_value: object):
        raise ValueError('deliberate')

    with pytest.raises(ValueError, match='deliberate'):
        window.draw(surface, app, {})
    assert _window.get() is None

    def replacement(input_value: object, added=5):
        return False, added

    app.__wrapped__.__code__ = replacement.__code__
    app.__wrapped__.__defaults__ = replacement.__defaults__
    assert window.draw(surface, app, {}) == (False, 5)
    window.close()


def test_returned_edits_reach_uncached_window_caller(monkeypatch):
    window, surface = host(monkeypatch)
    model = {'value': 10}

    @gui
    def child(input_value: int, view_events=None):
        return bool(view_events.get('plus')), input_value + int(bool(view_events.get('plus')))

    @gui(use_cache=False)
    def app(input_value: object):
        changed, model['value'] = child(model['value'])
        return changed, model['value']

    assert window.draw(surface, app, {}) == (False, 10)
    node = window.cache.graph.nodes()[0]
    window.cache.send(node, 'plus')
    assert window.draw(surface, app, {}) == (True, 11)
    assert window.draw(surface, app, {}) == (False, 11)
    window.close()


def test_root_space_starts_under_the_title_bar(monkeypatch):
    """A view at top=0 of the root sits below the window's 30 px title bar and one that takes the
    root's height ends at the surface's bottom, so no app adds the bar's height."""
    from meltygui.core.runtime.app import _root_body
    window, surface = host(monkeypatch)
    shown = []
    monkeypatch.setattr(window.cache, 'present', shown.append)

    @gui(listen=())
    def child(input_value: object):
        return None

    @gui(use_cache=False)
    def root(input_value: object, width=None, height=None):
        child(None, name='child', left=0., top=0., width=width, height=height)

    _root_body(root, 'native root', config={'view_kwargs': {}})(surface)
    (node,) = window.cache.root_ids
    assert shown == [(0., 30.)] and window.cache.geometry.inset == 30.
    assert tuple(window.cache.graph.rect(node)) == (0., 0., 400., 250.)
    window.close()


def test_root_is_never_under_one_pixel(monkeypatch):
    """A window the compositor made 40 by 10, lower than its 30 px title bar, still hands the root
    a size of at least 1 by 1 instead of a negative height (examples/gui_todo.py crashed)."""
    from meltygui.core.rendering.gui_native_collision import NativeCollision
    window, surface = host(monkeypatch)
    sizes = []

    @gui(use_cache=False)
    def root(input_value: object, width=None, height=None):
        sizes.append((width, height))

    window.draw(surface, root, {})
    native = window.cache.geometry.native = NativeCollision(window.cache.geometry)
    native.observe((((100., 140.), (100., 110.)), ((0., 1200.), (0., 900.)), 'feed', 1, ((), ())))
    window.draw(surface, root, {})
    window.draw(surface, root, {})
    assert sizes[0] == (400., 250.) and sizes[1:] == [(40., 1.), (40., 1.)]
    window.close()


def test_private_window_keeps_view_registry_out_of_session(monkeypatch):
    import meltygui.core.rendering.gui_window_prototype as module
    registry = {}
    found = []
    monkeypatch.setattr(module, '_GuiWindow', lambda state_registry: SimpleNamespace(
        draw=lambda *args: found.append(state_registry)))
    def view():
        pass
    for remember in (True, False):
        surface = SimpleNamespace(session=SimpleNamespace(draw_state_registry=registry))
        view.__gui_persist_views__ = remember
        module.draw_gui_surface(surface, view, {})
    assert found[0] is registry
    assert found[1] is None
