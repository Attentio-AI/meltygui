"""Suspension saves a live app without requiring window/interpreter teardown."""
import json
import threading

import pytest

from meltygui.core.runtime import app, app_session, app_settings, launch_override
from meltygui.core.windowing import glfw_utils


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    app_id = 'checkpoint-test'
    session = app_session.AppSession()
    session.app_state['counter'] = 1
    values = {'font_size': 14}
    settings = app_settings.AppSettings(values, 'Editor', app_settings.settings_path(app_id))
    monkeypatch.setattr(app, '_state', dict(app._state, failed=False, session=session, app_id=app_id))
    monkeypatch.setattr(app, '_ROOTS', [(lambda: None, {'settings': settings})])
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(launch_override, '_state', dict(
        launch_override._state, path=launch_override.overrides_path(app_id),
        overrides={}, dirty=True))
    # Pending source edits have their own disk-write integration test. This
    # fixture represents an app whose editor has no queued source edits.
    monkeypatch.setattr(app, '_flush_pending_saves', lambda: None)
    return app_id, session, values


def test_checkpoint_round_trips_state_while_live_objects_keep_their_identity(live_app):
    app_id, session, values = live_app
    assert app.checkpoint() is True
    assert app_session.load(app_id).app_state['counter'] == 1
    assert json.loads(app_settings.settings_path(app_id).read_text()) == {'Editor': {'font_size': 14}}
    assert launch_override.overrides_path(app_id).exists()
    assert launch_override._state['dirty'] is False
    session.app_state['counter'] = 2
    values['font_size'] = 20
    assert app.checkpoint() is True
    assert app._state['session'] is session
    assert app._ROOTS[0][1]['settings'].values is values
    assert app_session.load(app_id).app_state['counter'] == 2
    assert json.loads(app_settings.settings_path(app_id).read_text()) == {'Editor': {'font_size': 20}}


def test_failed_runtime_cannot_replace_the_last_good_checkpoint(live_app):
    app_id, session, values = live_app
    app.checkpoint()
    saved = app_session.session_path(app_id).read_bytes()
    session.app_state['counter'] = 999
    values['font_size'] = 999
    app._state['failed'] = True
    assert app.checkpoint() is False
    assert app_session.session_path(app_id).read_bytes() == saved
    assert json.loads(app_settings.settings_path(app_id).read_text()) == {'Editor': {'font_size': 14}}


def test_checkpoint_from_another_thread_fails_before_any_save(live_app):
    app_id, _, _ = live_app
    errors = []

    def save_from_worker():
        try:
            app.checkpoint()
        except RuntimeError as error:
            errors.append(str(error))

    worker = threading.Thread(target=save_from_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors and 'render thread' in errors[0]
    assert not app_session.session_path(app_id).exists()
    assert not app_settings.settings_path(app_id).exists()


def test_failed_pending_save_does_not_publish_a_new_session(live_app, monkeypatch):
    app_id, _, _ = live_app

    def cannot_save():
        raise OSError('workspace is unavailable')

    monkeypatch.setattr(app, '_flush_pending_saves', cannot_save)
    with pytest.raises(OSError, match='workspace is unavailable'):
        app.checkpoint()
    assert not app_session.session_path(app_id).exists()


@pytest.mark.parametrize('destination', ['session', 'settings', 'overrides'])
def test_checkpoint_reports_failed_disk_writes(live_app, destination):
    app_id, _, _ = live_app
    path = {'session': app_session.session_path(app_id),
            'settings': app_settings.settings_path(app_id),
            'overrides': launch_override.overrides_path(app_id)}[destination]
    # A regular file occupying the parent prevents a real write on any platform.
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    path.parent.write_text('not a directory')
    assert app.checkpoint() is False
    assert path.parent.is_file()
