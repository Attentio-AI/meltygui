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
    monkeypatch.setattr(window.cache, 'process_host_input', lambda: None)
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
