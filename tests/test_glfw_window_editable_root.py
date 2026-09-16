"""A meltygui app OUTSIDE the latent-descent checkout is editable source
(09-12): `@glfw_window` registers the decorated function's project root
(address.add_editable_root), so the app's own file loads into the code
hosts, `@glfw_window(tint=...)` shows up as the input source driving the
view's tint, and `locate_tint` writes land in that decorator. Re-running
the decorator (what a hotswap of the def does) updates the registered
root's config in place instead of adding a second window.

Run: venv/bin/python -m pytest tests/test_glfw_window_editable_root.py -q
"""
import importlib.util
import os
import sys
import textwrap
import time


import pytest

import meltygui.code.fileref as address  # noqa: E402
import meltygui.core.runtime.app as app  # noqa: E402


APP_SOURCE = textwrap.dedent('''
    from meltygui import glfw_window
    from meltygui.core.core_render import render_func

    SEEN = {}

    @glfw_window(name='Files', app_id='fake-browser', width=720, height=640, tint=(0.32, 0.42, 0.54))
    @render_func()
    def browser(_, draw_state):
        SEEN['ds'] = draw_state
        return False, None
''')


@pytest.fixture
def no_boot(monkeypatch):
    """Decorate without booting glfw / the import thread, and without the
    main-return hook; the roots list is restored after."""
    monkeypatch.setitem(app._state, 'booted', True)
    monkeypatch.setitem(app._state, 'hooked', True)
    monkeypatch.setitem(app._state, 'ran', False)
    saved = list(app._ROOTS)
    yield
    app._ROOTS[:] = saved


@pytest.fixture
def fresh_roots(monkeypatch):
    monkeypatch.setattr(address, "_EDITABLE_ROOTS", [address._PROJECT_ROOT])
    address._EDITABLE_SOURCE_CACHE.clear()
    yield
    address._EDITABLE_SOURCE_CACHE.clear()


def _write_app(tmp_path, source=APP_SOURCE, marker=".git"):
    (tmp_path / marker).mkdir()
    path = tmp_path / "fake_browser.py"
    path.write_text(source)
    return path


def _load(path, modname="fake_browser_under_test"):
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


# ── the gate ──────────────────────────────────────────────────────────────

def test_outside_file_is_refused_until_its_root_is_registered(tmp_path, fresh_roots):
    path = _write_app(tmp_path)
    assert not address.is_editable_source(path)
    root = address.add_editable_root(path)
    assert root == tmp_path.resolve()
    assert address.is_editable_source(path)
    assert address.editable_roots()[0] == address._PROJECT_ROOT
    assert root in address.editable_roots()


def test_project_root_is_the_nearest_marker_else_the_directory(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    f = nested / "x.py"
    f.write_text("")
    assert address.project_root_of(f) == tmp_path.resolve()
    bare = tmp_path.parent / (tmp_path.name + "_bare")
    bare.mkdir()
    g = bare / "y.py"
    g.write_text("")
    assert address.project_root_of(g) == bare.resolve()


def test_library_installs_are_never_registered(tmp_path, fresh_roots):
    lib = tmp_path / "venv" / "lib" / "site-packages" / "pkg"
    lib.mkdir(parents=True)
    f = lib / "mod.py"
    f.write_text("")
    assert address.add_editable_root(f) is None
    assert not address.is_editable_source(f)
    assert len(address.editable_roots()) == 1


def test_registering_twice_is_one_root(tmp_path, fresh_roots):
    path = _write_app(tmp_path)
    address.add_editable_root(path)
    address.add_editable_root(tmp_path)
    assert address.editable_roots().count(tmp_path.resolve()) == 1


# ── the decorator ─────────────────────────────────────────────────────────

def test_glfw_window_registers_its_projects_root(tmp_path, no_boot, fresh_roots):
    path = _write_app(tmp_path)
    module = _load(path)
    assert address.is_editable_source(path)
    fn, config = app._ROOTS[-1]
    assert fn is module.browser
    assert config['view_kwargs'] == {'tint': (0.32, 0.42, 0.54)}


def test_redecoration_updates_the_registered_root_in_place(no_boot, monkeypatch):
    before = len(app._ROOTS)

    @app.glfw_window(name='T', tint=(0.1, 0.2, 0.3), with_header=object())
    def editor():
        pass
    fn, config = app._ROOTS[-1]
    assert len(app._ROOTS) == before + 1

    monkeypatch.setitem(app._state, 'ran', True)      # the window is open

    @app.glfw_window(name='T', width=640, height=480, tint=(0.9, 0.8, 0.7))   # the hotswap's re-run
    def editor():
        pass
    assert len(app._ROOTS) == before + 1               # no second window
    assert app._ROOTS[-1][1] is config                 # the same live config object
    assert app._ROOTS[-1][0] is fn                     # the open window keeps its body
    assert (config['width'], config['height']) == (640, 480) and config['view_kwargs'] == {'tint': (0.9, 0.8, 0.7)}


def test_render_func_root_body_reads_the_live_config(monkeypatch):
    seen = {}

    def fake_draw_root(fn, name=None, value=None, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(app, "_draw_root", fake_draw_root)

    def body(_, draw_state):
        return False, None
    body.__render_func__ = True
    config = {'view_kwargs': {'tint': (0.1, 0.2, 0.3)}}
    draw = app._root_body(body, "panels", config['view_kwargs'], config=config)
    draw(None)
    assert seen == {'tint': (0.1, 0.2, 0.3)}
    config['view_kwargs'] = {'tint': (0.5, 0.5, 0.5), 'show_name': True}   # the re-decoration
    draw(None)
    assert seen == {'tint': (0.5, 0.5, 0.5), 'show_name': True}


# ── end to end: the app's decorator is the tint's source ─────────────────

def test_outside_app_decorator_is_the_tint_source(tmp_path, no_boot, fresh_roots):
    """The 09-12 report: the file browser's header tint chip could not
    locate its `@glfw_window(tint=...)`. Render the app's root under the
    GL harness, pump the code hosts until its file has parsed, and ask the
    framework which source drives `tint`."""
    from conftest import _ensure_gl_context, begin_frame, end_frame
    import profile_render_wrapper as H
    _ensure_gl_context()
    H._init_melty()
    from meltygui.core.melty import Melty
    from meltygui.core.rendering.parameter_core import get_source_for
    from meltygui.core.rendering.parameter_core import _sources_for

    path = _write_app(tmp_path)
    module = _load(path)
    fn, config = app._ROOTS[-1]

    def frame():
        H._tick()
        begin_frame()
        fn(None, name='files', **config['view_kwargs'])
        for host in list(Melty.render_hosts.values()):
            if host.draw_needed():
                host.draw()
        end_frame()

    frame(); frame()
    ds = module.SEEN['ds']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        frame()
        time.sleep(0.02)
        sources = _sources_for(ds)
        if "glfw window decoration" in sources["kinds"].values():
            break
    else:
        pytest.fail("the app's @glfw_window never registered as an input source: "
                    f"{sorted(sources['kinds'].values())}")
    name = "@glfw_window(browser)"
    assert sources["sources"][name].get("tint") == (0.32, 0.42, 0.54)
    assert sources["locations"][name][0] == str(path)
    assert get_source_for("tint", ds) == name

    # A locate_ write reaches the root's live config at once (anywhere.live_apply),
    # before the save + recompile trip the same write triggers.
    ds.locate_tint = (0.1, 0.2, 0.3)
    assert config["view_kwargs"]["tint"] == (0.1, 0.2, 0.3)
    assert _sources_for(ds)["sources"][name].get("tint") == (0.1, 0.2, 0.3)
    frame()
    assert ds._kwargs.get("tint") == (0.1, 0.2, 0.3)
