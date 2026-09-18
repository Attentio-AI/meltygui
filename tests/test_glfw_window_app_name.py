"""`@glfw_window(app_name=...)` names the PROCESS: the first window registered
with one wins (that is what the user already sees in a system monitor), a
later different name is ignored with a warning, and the same name again is
silent (a hotswap re-runs the decorator).

Run: venv/bin/python -m pytest tests/test_glfw_window_app_name.py -q
"""
import os
import sys

import pytest

import meltygui.core.runtime.app as app


@pytest.fixture
def no_boot(monkeypatch):
    monkeypatch.setitem(app._state, 'booted', True)
    monkeypatch.setitem(app._state, 'hooked', True)
    monkeypatch.setitem(app._state, 'ran', False)
    monkeypatch.setitem(app._state, 'app_name', None)
    saved = list(app._ROOTS)
    yield
    app._ROOTS[:] = saved


@pytest.fixture
def named(monkeypatch):
    """Record the names handed to the OS instead of renaming the test runner."""
    names = []
    monkeypatch.setattr(app, '_set_process_name', names.append)
    return names


def test_first_window_names_the_process_and_conflicts_warn(no_boot, named, capsys):
    @app.glfw_window(name='Editor', app_name='meltycodeedit')
    def editor(): pass

    @app.glfw_window(name='Editor', app_name='meltycodeedit')
    def editor(): pass  # noqa: F811  the hotswap re-decoration

    @app.glfw_window(name='Palette')
    def palette(): pass

    assert named == ['meltycodeedit'] and app._state['app_name'] == 'meltycodeedit'
    assert capsys.readouterr().err == ''

    @app.glfw_window(name='Console', app_name='other-app')
    def console(): pass

    assert named == ['meltycodeedit'] and app._state['app_name'] == 'meltycodeedit'
    assert "app_name 'other-app' ignored" in capsys.readouterr().err


def test_no_app_name_leaves_the_process_alone(no_boot, named):
    @app.glfw_window(name='Plain')
    def plain(): pass

    assert named == [] and app._state['app_name'] is None


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='prctl is Linux')
def test_process_name_reaches_the_kernel_and_is_shortened(capsys):
    def comm():
        with open(f'/proc/{os.getpid()}/comm') as stream:
            return stream.read().strip()
    before = comm()
    try:
        app._set_process_name('melty-tests')
        assert comm() == 'melty-tests' and capsys.readouterr().err == ''
        app._set_process_name('meltycodeeditor-long')
        assert comm() == 'meltycodeeditor' and 'shortened' in capsys.readouterr().err
    finally:
        app._set_process_name(before)
