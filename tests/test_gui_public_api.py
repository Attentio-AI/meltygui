"""Public prototype decorators: one import, one tag, optional input and return."""
import inspect

import pytest

from meltygui import gui, os_window
from meltygui.core.runtime import app
from meltygui.core.conversion.dict_conversion import DictConversion
from test_gui_window_prototype import host


@pytest.fixture
def registrations(monkeypatch):
    monkeypatch.setattr(app, 'boot', lambda app_id=None: None)
    monkeypatch.setattr(app, 'name_app', lambda name=None: None)
    monkeypatch.setattr(app, '_register_editable', lambda source: None)
    monkeypatch.setattr(app, '_ROOTS', [])
    monkeypatch.setitem(app._state, 'hooked', True)
    monkeypatch.setitem(app._state, 'ran', False)
    return app._ROOTS


@pytest.mark.parametrize('decorate', [os_window, lambda **kw: gui(glfw_window=True, **kw)])
def test_os_window_is_gui_host_shorthand(registrations, monkeypatch, decorate):
    calls = []
    close = lambda surface: None

    @decorate(name='Minimal', width=640, height=480, app_id='minimal',
              on_close=close, completely_new_argument=19)
    def minimal(completely_new_argument=0, draw_state=None):
        calls.append((completely_new_argument, draw_state.width, draw_state.height))

    assert list(inspect.signature(minimal).parameters) == ['completely_new_argument', 'draw_state']
    assert len(registrations) == 1
    registered, config = registrations[0]
    assert registered is minimal
    assert (config['name'], config['width'], config['height'], config['on_close']) == ('Minimal', 640, 480, close)
    assert config['view_kwargs'] == {'completely_new_argument': 19}
    window, surface = host(monkeypatch)
    try:
        body = app._root_body(minimal, 'Minimal', config=config)
        # The first parameter is the view's value: a body that returns nothing hands it back.
        assert body(surface) == (False, 19)
        body(surface)
        assert calls == [(19, 400., 250.)]  # host size wins over initial window geometry
        config['view_kwargs']['completely_new_argument'] = 22
        body(surface)
        assert calls[-1] == (22, 400., 250.)
    finally:
        window.close()


def test_bare_decorators_and_user_minimal_example(registrations, monkeypatch):
    import meltygui_imgui as imgui
    text = []
    monkeypatch.setattr(imgui, 'text', text.append)  # this fixture uses graphics=False
    @os_window
    def bare():
        pass

    @os_window()
    def parentheses():
        pass

    # Keep the minimal API contract independent of the evolving table demo.
    @os_window(name='example main', width=1400, height=1000)
    def minimal():
        imgui.text('hello')
    assert [config['name'] for _, config in registrations] == ['bare', 'parentheses', 'example main']
    assert str(inspect.signature(minimal)) == '()'
    window, surface = host(monkeypatch)
    try:
        assert window.draw(surface, minimal, {}) == (False, None)
        assert text == ['hello']
    finally:
        window.close()


@pytest.mark.parametrize('cached', [True, False])
def test_no_return_hands_back_the_value_which_is_injected_when_nothing_is_passed(monkeypatch, cached):
    class LocalState(DictConversion):
        def __init__(self):
            super().__init__()
            self.count = 0

    seen = []

    @gui(use_cache=cached)
    def view(state: LocalState = None, custom_argument='default'):
        state.count += 1
        seen.append((state, custom_argument))

    window, surface = host(monkeypatch)
    own = LocalState()
    try:
        result = window.draw(surface, view, {'custom_argument': 'caller'})
        assert result[0] is False and result[1] is seen[0][0] and type(result[1]) is LocalState
        window.draw(surface, view, {'custom_argument': 'new argument'})
        assert seen[0][0] is seen[1][0]
        assert seen[1][0].count == 2
        assert [v for _, v in seen] == ['caller', 'new argument']
        # A value the host passes takes the injected one's place, which is kept for when it stops.
        result = window.draw(surface, view, {'value': own, 'custom_argument': 'passed'})
        assert result[1] is own and seen[-1][0] is own and own.count == 1
        window.draw(surface, view, {'custom_argument': 'again'})
        assert seen[-1][0] is seen[0][0] and seen[0][0].count == 3
    finally:
        window.close()


def test_drawing_only_child_composes_with_explicit_editable_returns(monkeypatch):
    @gui
    def decoration(draw_state=None):
        pass

    @gui
    def editable(input_value: int):
        return True, input_value + 1

    @gui(use_cache=False)
    def root():
        decoration()
        return editable(2)

    window, surface = host(monkeypatch)
    try:
        assert window.draw(surface, root, {}) == (True, 3)
        assert window.draw(surface, root, {}) == (False, 3)
    finally:
        window.close()


def test_plain_gui_does_not_register_a_window(registrations):
    @gui
    def component():
        pass
    assert registrations == []


def test_public_import_does_not_boot_or_load_native_extension():
    from test_startup_imports import imported_after, RENDER_LIBRARIES, CODE_STACK, GL_MODULES
    loaded = imported_after('from meltygui import gui, os_window',
                            'assert callable(gui) and callable(os_window)')
    assert 'meltygui.core.rendering._gui_native' not in loaded
    assert not loaded & set(RENDER_LIBRARIES + CODE_STACK + GL_MODULES)
