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
