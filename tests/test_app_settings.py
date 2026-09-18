"""`@glfw_window(settings={...})`: a plain dict of defaults the framework
loads in place from `$XDG_CONFIG_HOME/<app_id>/settings.json`, merges by
the dict's own schema (nested dicts as sub-folders), saves per window
section, and shows as a cog in the title bar (titlebar "settings" kind,
on the inner side of the group holding the close button).

Run: .venv/bin/pytest tests/test_app_settings.py -q
"""
import json

import pytest

import meltygui.core.runtime.app as app
import meltygui.core.runtime.app_settings as app_settings


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    return tmp_path


@pytest.fixture
def no_boot(monkeypatch):
    monkeypatch.setitem(app._state, 'booted', True)
    monkeypatch.setitem(app._state, 'hooked', True)
    monkeypatch.setitem(app._state, 'ran', False)
    monkeypatch.setitem(app._state, 'app_id', 'settings-test')
    saved = list(app._ROOTS)
    yield
    app._ROOTS[:] = saved


def test_merge_keeps_the_defaults_as_the_schema():
    defaults = {'size': 14, 'wrap': False, 'name': 'x', 'ratio': 1.0, 'accent': (0.1, 0.2, 0.3),
                'editor': {'tabs': 4, 'inner': {'deep': 'a'}}, 'anything': None}
    saved = {'size': 20, 'wrap': 1, 'name': 3, 'ratio': 2, 'unknown': 'dropped', 'accent': [0.5, 0.5, 0.5],
             'editor': {'tabs': 'no', 'inner': {'deep': 'b', 'extra': 1}, 'gone': 5}, 'anything': [1]}
    merged = app_settings.merge_saved(defaults, saved)
    assert merged is defaults
    assert defaults == {'size': 20, 'wrap': False, 'name': 'x', 'ratio': 2, 'accent': (0.5, 0.5, 0.5),
                        'editor': {'tabs': 4, 'inner': {'deep': 'b'}}, 'anything': [1]}
    assert isinstance(defaults['accent'], tuple)
    # A section that is not a dict (a hand-edited file) leaves the defaults alone.
    assert app_settings.merge_saved({'a': 1}, 'junk') == {'a': 1}


def test_settings_load_in_place_and_save_their_own_section(config_home):
    path = app_settings.settings_path('demo')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'Editor': {'size': 20, 'editor': {'tabs': 8}}, 'Other': {'keep': True}}))
    values = {'size': 14, 'editor': {'tabs': 4, 'wrap': False}}
    settings = app_settings.AppSettings(values, 'Editor', path)
    assert settings.values is values
    assert values == {'size': 20, 'editor': {'tabs': 8, 'wrap': False}}

    values['editor']['wrap'] = True
    assert settings.save() == path
    on_disk = json.loads(path.read_text())
    assert on_disk == {'Editor': {'size': 20, 'editor': {'tabs': 8, 'wrap': True}}, 'Other': {'keep': True}}
    assert not list(path.parent.glob('*.tmp-*'))


def test_broken_file_is_moved_aside_and_defaults_win(config_home, capsys):
    path = app_settings.settings_path('demo')
    path.parent.mkdir(parents=True)
    path.write_text('{not json')
    settings = app_settings.AppSettings({'size': 14}, 'Editor', path)
    assert settings.values == {'size': 14}
    assert not path.exists() and list(path.parent.glob('settings.json.broken-*'))
    assert 'cannot read settings' in capsys.readouterr().err


def test_decorator_loads_the_dict_and_keeps_it_across_redecoration(config_home, no_boot):
    path = app_settings.settings_path('settings-test')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'Editor': {'size': 20}}))
    values = {'size': 14, 'editor': {'tabs': 4}}

    @app.glfw_window(name='Editor', settings=values)
    def editor(): pass

    assert values == {'size': 20, 'editor': {'tabs': 4}}
    config = next(kw for fn, kw in app._ROOTS if kw['name'] == 'Editor')
    first = config['settings']
    assert first.values is values and first.section == 'Editor' and first.path == path

    @app.glfw_window(name='Editor', settings=values)
    def editor(): pass  # noqa: F811  the hotswap re-decoration: same dict, same object

    assert config['settings'] is first

    fresh = {'size': 1}

    @app.glfw_window(name='Editor', settings=fresh)
    def editor(): pass  # noqa: F811  a new dict loads afresh

    assert config['settings'] is not first and config['settings'].values is fresh and fresh == {'size': 20}

    @app.glfw_window(name='Plain')
    def plain(): pass

    assert next(kw for fn, kw in app._ROOTS if kw['name'] == 'Plain')['settings'] is None
    with pytest.raises(TypeError):
        app.glfw_window(name='Wrong', settings=object())(lambda: None)


def test_exit_save_writes_every_window_section(config_home, no_boot):
    values = {'size': 14}

    @app.glfw_window(name='Editor', settings=values)
    def editor(): pass

    values['size'] = 33
    app._save_settings()
    assert json.loads(app_settings.settings_path('settings-test').read_text()) == {'Editor': {'size': 33}}


class _Surface:
    def __init__(self, settings):
        self.settings = settings


@pytest.fixture
def chrome(monkeypatch):
    import meltygui.core.windowing.titlebar as titlebar
    from meltygui.core.runtime.toggles import Toggles
    monkeypatch.setattr(Toggles.Melty, 'titlebar_move_toggle', True)
    monkeypatch.setattr(titlebar.hypr_left_drag, 'available', lambda: True)
    return titlebar


def test_settings_cog_sits_inside_the_close_group(chrome, monkeypatch):
    from meltygui.core.windowing.surface import Surface
    titlebar = chrome
    monkeypatch.setattr(titlebar, 'button_layout', lambda: ((), ('minimize', 'close')))
    monkeypatch.setattr(Surface, 'active', _Surface(None))
    assert titlebar.control_kinds() == ((), ('move', 'minimize', 'close'))
    monkeypatch.setattr(Surface, 'active', _Surface(object()))
    assert titlebar.control_kinds() == ((), ('settings', 'move', 'minimize', 'close'))
    monkeypatch.setattr(titlebar, 'button_layout', lambda: (('close', 'minimize'), ()))
    assert titlebar.control_kinds() == (('close', 'minimize', 'move', 'settings'), ())
    monkeypatch.setattr(Surface, 'active', None)
    assert titlebar.control_kinds() == (('close', 'minimize', 'move'), ())


def test_settings_button_asks_the_active_surface_to_open(chrome, monkeypatch):
    from meltygui.core.windowing.surface import Surface
    titlebar = chrome
    opened = []

    class Settings:
        def request_open(self):
            opened.append(True)

    monkeypatch.setattr(Surface, 'active', _Surface(Settings()))
    titlebar._activate_button('settings', None)
    assert opened == [True]
    monkeypatch.setattr(Surface, 'active', None)
    titlebar._activate_button('settings', None)      # no surface: nothing to open, no error
    assert opened == [True]
