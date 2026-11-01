"""Embedded view imports leave application lifecycle and saved sessions alone."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import meltygui
from meltygui.core.runtime import app


@pytest.mark.parametrize('booted', [False, True])
def test_imports_leave_host_lifecycle_unchanged(monkeypatch, booted):
    state = dict(app._state, booted=booted, ran=booted, presented=booted)
    monkeypatch.setattr(app, '_state', state.copy())
    roots, callbacks = list(app._ROOTS), list(app._AFTER_FIRST_FRAME)
    monkeypatch.setattr(app, '_ROOTS', roots.copy())
    monkeypatch.setattr(app, '_AFTER_FIRST_FRAME', callbacks.copy())
    startup = Mock(side_effect=AssertionError('must not start a second host'))
    monkeypatch.setattr(app, '_utf8_output', startup)
    monkeypatch.setattr(app, '_unhook_main_return', startup)
    callback = Mock()

    def view():
        return 'embedded view'

    with meltygui.embedded_imports():
        meltygui.boot(app_id='source-app')
        assert meltygui.glfw_window(view, name='Source', app_name='source-app') is view
        meltygui.after_first_frame(callback)
        meltygui.run()

    assert view() == 'embedded view'
    assert app._state == state
    assert app._ROOTS == roots
    assert app._AFTER_FIRST_FRAME == callbacks
    startup.assert_not_called()
    callback.assert_not_called()


def test_models_are_retained_within_scope_and_separated_by_app(monkeypatch):
    load_session = Mock(side_effect=AssertionError('must not open saved sessions'))
    monkeypatch.setattr(app, '_load_session', load_session)
    factory = Mock(return_value=[])
    with meltygui.embedded_imports():
        first = meltygui.persisted('files', factory, app_id='editor')
        first.append('preview-only')
        with meltygui.embedded_imports():
            assert meltygui.persisted('files', factory, app_id='editor') is first
        assert meltygui.persisted('files', list, app_id='other') == []
        assert meltygui.persisted('layout', dict, app_id='editor') == {}
    with meltygui.embedded_imports():
        assert meltygui.persisted('files', list, app_id='editor') == []
    factory.assert_called_once_with()
    load_session.assert_not_called()


def test_failure_restores_normal_persistence_and_other_threads_are_unaffected(monkeypatch):
    saved = ['existing app state']
    session = SimpleNamespace(app_state={'files': saved})
    monkeypatch.setattr(app, 'boot', lambda app_id: None)
    monkeypatch.setattr(app, '_load_session', lambda: session)
    with pytest.raises(ValueError, match='import failed'):
        with meltygui.embedded_imports():
            assert meltygui.persisted('files', list) == []
            with ThreadPoolExecutor(max_workers=1) as worker:
                assert worker.submit(meltygui.persisted, 'files', list).result() is saved
            raise ValueError('import failed')
    assert meltygui.persisted('files', list) is saved
    assert saved == ['existing app state']
