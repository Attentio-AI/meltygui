"""Run on a reserved desktop: native @gui views nested in legacy @render_func
views through the path-finding bridge (core/rendering/gui_compat.py)."""
from types import SimpleNamespace

import pytest
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from meltygui import gui
from meltygui.core import core_render
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty, EmbeddedFrameState
from meltygui.core.rendering import gui_compat


@pytest.fixture
def host(monkeypatch):
    """A legacy frame environment whose cached views and @gui views go
    through one bridge, as an OS window does under MELTY_RUST=1."""
    _ensure_gl_context()
    io = imgui.get_io()
    io.mouse_pos = (-100, -100)
    for index in range(len(io.mouse_down)):
        io.mouse_down[index] = False
    monkeypatch.setattr(core_render, '_rust_route', gui_compat.route)
    monkeypatch.setattr(core_render, '_rust_pass', gui_compat.PASS)
    state = EmbeddedFrameState()
    state.values['cache'] = gui_compat.tile_cache()
    surface = SimpleNamespace(impl=SimpleNamespace(gui_character_callback=None), requests=[])
    surface.request_frame = lambda: surface.requests.append(Melty.frame_count)
    bridge = gui_compat.Bridge(surface)
    state.values['cache'].bridge = bridge
    yield SimpleNamespace(bridge=bridge, state=state, surface=surface)
    bridge.close()
    state.close()


def frame(host, body, position=(-100, -100), *, down=False):
    io = imgui.get_io()
    io.delta_time = 1 / 60
    io.mouse_pos = position
    io.mouse_down[0] = down
    imgui.new_frame()
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(*io.display_size)
    imgui.begin('legacy host', flags=imgui.WINDOW_NO_DECORATION | imgui.WINDOW_NO_SAVED_SETTINGS)
    try:
        with Melty.embedded_frame(host.state):
            host.bridge.begin()
            try:
                return body()
            finally:
                host.bridge.finish()
    finally:
        imgui.end()
        imgui.render()


def click(host, body, position):
    for down in (False, True, False, False):
        result = frame(host, body, position, down=down)
    return result


def test_native_views_nest_in_legacy_views_and_only_run_when_needed(host):
    runs = {'leaf': 0, 'panel': 0, 'inner': 0}
    seen = {}

    @gui(width=120, height=30)
    def inner(input_value):
        runs['inner'] += 1
        imgui.text('inner')

    @gui(width=200, height=None)
    def leaf(input_value, clicks=0, draw_state=None):
        runs['leaf'] += 1
        seen['button'] = tuple(imgui.get_cursor_screen_pos())
        if imgui.button('press', 80, 24):
            draw_state.clicks = clicks + 1
        seen['clicks'] = draw_state.clicks
        inner(None, name='nested')

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        runs['panel'] += 1
        seen['origin'] = tuple(imgui.get_cursor_screen_pos())
        leaf('in a cached legacy view', name='leaf')
        return False, input_value

    def body():
        panel(1, name='panel', width=300, height=120)
        leaf('at the root', name='root leaf')

    for _ in range(4):
        frame(host, body)
    settled = dict(runs)
    assert settled['leaf'] >= 2 and settled['inner'] >= 2
    # Idle frames: the legacy root declares everything again, nothing executes.
    for _ in range(3):
        frame(host, body)
    assert runs == settled
    # The body draws in its own coordinates: (0, 0) is the view's corner.
    assert seen['button'] == (0.0, 0.0)
    nodes = [record.node for record in host.bridge.native_keys.values()]
    assert len(nodes) == 2 and all(node is not None for node in nodes)


def test_click_reaches_a_native_view_and_its_edit_reaches_the_legacy_caller(host):
    model = {'count': 0}
    runs = {'panel': 0, 'stepper': 0}
    where = {}

    @gui(width=200, height=40)
    def stepper(input_value: int):
        runs['stepper'] += 1
        if imgui.button('add one', 100, 24):
            return True, input_value + 1

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        runs['panel'] += 1
        where['stepper'] = tuple(imgui.get_cursor_screen_pos())
        changed, value = stepper(model['count'], name='stepper')
        if changed:
            model['count'] = value
        return False, input_value

    def body():
        panel(1, name='panel', width=300, height=120)

    for _ in range(4):
        frame(host, body)
    x, y = where['stepper']
    click(host, body, (x + 20, y + 10))
    for _ in range(3):
        frame(host, body, (x + 20, y + 10))
    assert model['count'] == 1
    # The edit woke the legacy caller: a frame was requested for it.
    assert host.surface.requests


def test_native_edit_wakes_a_clean_legacy_caller(host):
    model = {'count': 0}
    fire = {'now': False}
    runs = {'panel': 0}

    @gui(width=200, height=40)
    def child(input_value: int):
        if fire['now']:
            fire['now'] = False
            return True, input_value + 1
        imgui.text(str(input_value))

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        runs['panel'] += 1
        changed, value = child(model['count'], name='child')
        if changed:
            model['count'] = value
        return False, input_value

    @render_func(use_cache=True)
    def outer(input_value, draw_state):
        panel(1, name='panel', width=300, height=120)
        return False, input_value

    def body():
        outer(1, name='outer', width=320, height=160)

    for _ in range(5):
        frame(host, body)
    settled = runs['panel']
    for _ in range(2):
        frame(host, body)
    assert runs['panel'] == settled
    # As a model change would: Rust runs the native child on its own. Both
    # cached legacy callers above it are clean and have to be woken.
    record = next(iter(host.bridge.native_keys.values()))
    fire['now'] = True
    host.surface.requests.clear()
    host.bridge.cache.invalidate_id(record.node)
    for _ in range(3):
        frame(host, body)
    assert model['count'] == 1 and runs['panel'] > settled
    assert host.surface.requests


def test_legacy_view_inside_a_native_view_raises_and_the_host_recovers(host):
    @render_func(use_cache=False)
    def legacy(input_value, draw_state):
        imgui.text('legacy')
        return False, input_value

    @gui(width=200, height=40)
    def native(input_value):
        legacy(1, name='child')

    shown = {'native': True}

    def body():
        if shown['native']:
            native(None, name='native')
        imgui.text('after')

    with pytest.raises(RuntimeError, match='cannot be drawn inside a retained @gui view'):
        frame(host, body)
    shown['native'] = False
    for _ in range(3):
        frame(host, body)
    assert host.bridge.cache.stack == []


def test_immediate_native_view_runs_with_its_legacy_caller(host):
    runs = {'immediate': 0, 'panel': 0}

    @gui(use_cache=False)
    def immediate(input_value):
        runs['immediate'] += 1
        imgui.text(str(input_value))

    @render_func(use_cache=False)
    def panel(input_value, draw_state):
        runs['panel'] += 1
        immediate('a')
        immediate('b')
        return False, input_value

    def body():
        panel(1, name='panel', width=300, height=80)

    for _ in range(3):
        frame(host, body)
    assert runs['panel'] == 3 and runs['immediate'] == 6


def test_idle_top_level_views_give_their_textures_back(host):
    """Switching what a window shows (the editor's tile types) must not pile
    up top-level nodes: each holds a texture the size of the surface."""
    bridge, shown = host.bridge, [0]

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        imgui.text(f'panel {input_value}')
        return False, input_value

    def body():
        panel(shown[0], name=f'panel {shown[0]}', width=200, height=60)

    nodes = []
    for index in range(gui_compat.IDLE_ROOTS + 5):
        shown[0] = index
        known = set(bridge.nodes)
        frame(host, body)
        frame(host, body)
        nodes.extend(set(bridge.nodes) - known)
    assert len(nodes) == gui_compat.IDLE_ROOTS + 5
    kept = [node for node in nodes if node in bridge.nodes]
    assert kept == nodes[-(gui_compat.IDLE_ROOTS + 1):]     # the one on screen and the newest idle ones
    gpu = bridge.cache.gpu
    assert all(gpu.texture(node) is not None for node in kept)
    assert all(gpu.texture(node) is None for node in nodes if node not in kept)
    # A dropped view comes back as a fresh node.
    shown[0] = 0
    frame(host, body)
    assert len(set(bridge.nodes) - set(nodes)) == 1
