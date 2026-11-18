"""The IDE completes from the stubs `stubs.py` writes; they must match the sources and the runtime."""
import importlib.util
from pathlib import Path

import pytest

import meltygui

SCRIPT = Path(meltygui.__file__).parents[1] / 'stubs.py'
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason='stubs.py lives in the checkout, not the wheel')


def load():
    spec = importlib.util.spec_from_file_location('melty_stubs', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stubs_are_current():
    assert load().stale() == [], 'run .venv/bin/python stubs.py'


def test_every_gui_view_is_published():
    import meltygui.rust
    assert meltygui.rust.VIEWS == load().gui_views()
    assert 'draw_editor' in meltygui.rust.VIEWS


def test_view_modules_load_on_request():
    assert meltygui.view.button_view.__name__ == 'meltygui.view.button_view'
    assert meltygui.rust.draw_button is meltygui.view.button_view.draw_button
    with pytest.raises(AttributeError):
        meltygui.view.missing_view
    with pytest.raises(AttributeError):
        meltygui.missing_name


def test_framework_names_cover_what_a_call_carries():
    from meltygui.core.rendering.gui_prototype import FILLED
    stubs = load()
    assert set(stubs.FRAMEWORK) | set(stubs.INJECTED) | set(stubs.REJECTED) == FILLED


def test_rust_stub_declares_each_view_as_called():
    """The IDE's parameter hint: the view's own parameters in the order they bind by position, then the framework's."""
    import ast
    import inspect
    import meltygui.rust
    stubs = load()
    declared = {node.name: node.args for node in ast.parse(Path(meltygui.rust.__file__).with_suffix('.pyi').read_text()).body
                if isinstance(node, ast.FunctionDef)}
    assert set(declared) == set(meltygui.rust.VIEWS) | {'draw_any'}
    for name, arguments in declared.items():
        own = [parameter for parameter in inspect.signature(inspect.unwrap(getattr(meltygui.rust, name))).parameters.values()
               if parameter.kind is not parameter.VAR_KEYWORD and parameter.name not in stubs.INJECTED
               and parameter.name not in stubs.FRAMEWORK]
        named = [argument.arg for argument in arguments.kwonlyargs]
        assert [argument.arg for argument in arguments.args] + named[:-len(stubs.FRAMEWORK)] == [p.name for p in own], name
        assert named[-len(stubs.FRAMEWORK):] == list(stubs.FRAMEWORK), name
