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
from meltygui.core.rendering import fast_view, gui_compat


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
    monkeypatch.setattr(fast_view, 'needs_wrapper', gui_compat.in_native_body)
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


def island_app(seen, runs, model, *, cached=True):
    """A native shell at the root with one legacy view inside it."""
    @render_func(use_cache=cached)
    def child(input_value, draw_state):
        runs['child'] += 1
        seen['cursor'] = tuple(imgui.get_cursor_screen_pos())
        seen['state'] = draw_state
        imgui.text(f'legacy {input_value}')
        seen['button'] = tuple(imgui.get_cursor_screen_pos())
        if imgui.button('press', 80, 24) or seen.get('key'):
            return True, input_value + 1
        return False, input_value

    @gui(width=260, height=120)
    def shell(input_value: dict):
        runs['shell'] += 1
        imgui.text('native shell')
        seen['local'] = tuple(imgui.get_cursor_screen_pos())
        changed, value = child(input_value['count'], name='child', width=200, height=70)
        seen['after'] = tuple(imgui.get_cursor_screen_pos())
        if changed:
            input_value['count'] = value
            return True, input_value

    def body():
        if seen.pop('invalidate', False):
            seen['state'].invalidate()      # what a legacy model change does
        imgui.dummy(40, seen.get('gap', 30))
        seen['shell'] = tuple(imgui.get_cursor_screen_pos())
        shell(model, name='shell')
    return body


def test_legacy_view_in_a_native_view_draws_in_window_coordinates(host):
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model)
    for _ in range(5):
        frame(host, body)
    settled = dict(runs)
    assert settled['child'] >= 2
    # The native body is at (0, 0) in its own coordinates; the legacy body
    # sees the same spot as a window position.
    assert seen['cursor'] == (seen['shell'][0] + seen['local'][0], seen['shell'][1] + seen['local'][1])
    assert seen['cursor'] != seen['local']
    # The native caller's layout moved on by what the legacy view measured.
    assert seen['after'][1] > seen['local'][1]
    entry = next(entry for entry in host.bridge.entries.values() if entry.site is not None)
    assert host.bridge.cache.graph.parent(entry.node) == entry.host
    # Idle frames run nothing.
    for _ in range(3):
        frame(host, body)
    assert runs == settled


def test_legacy_view_in_a_native_view_runs_without_its_caller(host):
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model)
    for _ in range(5):
        frame(host, body)
    settled = dict(runs)
    # A legacy invalidation reaches the legacy view; its native caller stays clean.
    seen['invalidate'] = True
    frame(host, body)
    assert runs == {'shell': settled['shell'], 'child': settled['child'] + 1}
    # The pointer over it: the legacy hover rule runs it every frame, and the
    # native view below does not take the pointer.
    x, y = seen['button']
    for _ in range(3):
        frame(host, body, (x + 20, y + 10))
    assert runs['child'] >= settled['child'] + 3 and runs['shell'] == settled['shell']
    assert host.bridge.cache.imgui_input.hover is None


def test_legacy_edit_in_a_native_view_reaches_the_native_caller(host):
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model)
    for _ in range(5):
        frame(host, body)
    x, y = seen['button']
    click(host, body, (x + 20, y + 10))
    for _ in range(3):
        frame(host, body, (x + 20, y + 10))
    assert model['count'] == 1
    # The legacy view was drawn again with the caller's new value.
    assert host.bridge.entries[seen['state'].unique].input == 1


def test_legacy_view_reads_a_frames_events_once(host):
    """However often its native caller runs in a frame (to take an edit, then
    to show it), a legacy view runs once: again, it would read the same key again."""
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model)
    for _ in range(5):
        frame(host, body)
    seen['key'] = True      # a key held over four frames
    for _ in range(4):
        before = runs['child']
        seen['invalidate'] = True       # the event system wakes its subscriber
        frame(host, body)
        assert runs['child'] == before + 1
    seen['key'] = False
    for _ in range(4):
        frame(host, body)
    assert model['count'] == 4


def test_legacy_view_follows_its_native_caller(host):
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model)
    for _ in range(5):
        frame(host, body)
    settled, before = dict(runs), seen['cursor']
    # The native view moves; its body does not run, the legacy geometry inside follows.
    seen['gap'] = 75
    for _ in range(3):
        frame(host, body)
    assert seen['cursor'] == (before[0], before[1] + 45)
    assert runs['shell'] == settled['shell'] and runs['child'] > settled['child']
    assert host.bridge.cache.position(host.bridge.entries[seen['state'].unique].node) == (0., 0.)


def test_uncached_legacy_view_in_a_native_view_runs_with_its_caller(host):
    seen, runs, model = {}, {'shell': 0, 'child': 0}, {'count': 0}
    body = island_app(seen, runs, model, cached=False)
    for _ in range(5):
        frame(host, body)
    settled = dict(runs)
    for _ in range(3):
        frame(host, body)
    assert runs == settled
    host.bridge.cache.invalidate(model)
    frame(host, body)
    frame(host, body)
    assert runs['shell'] == settled['shell'] + 1 and runs['child'] >= settled['child'] + 1


def test_legacy_and_native_views_alternate(host):
    """legacy panel -> native shell -> legacy group -> native leaf."""
    runs = {'panel': 0, 'shell': 0, 'group': 0, 'leaf': 0}
    seen = {}

    @gui(width=120, height=30)
    def leaf(input_value, clicks=0, draw_state=None):
        runs['leaf'] += 1
        seen['leaf'] = cache_position(host)
        if imgui.button('leaf', 80, 24):
            draw_state.clicks = clicks + 1
        seen['clicks'] = draw_state.clicks

    @render_func(use_cache=True)
    def group(input_value, draw_state):
        runs['group'] += 1
        seen['group'] = draw_state
        imgui.text('legacy group')
        seen['leaf cursor'] = tuple(imgui.get_cursor_screen_pos())
        leaf(None, name='leaf')
        return False, input_value

    @gui(width=260, height=110)
    def shell(input_value):
        runs['shell'] += 1
        imgui.text('native shell')
        group(1, name='group', width=220, height=80)

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        runs['panel'] += 1
        imgui.text('legacy panel')
        shell(None, name='shell')
        return False, input_value

    def body():
        if seen.pop('invalidate', False):
            seen['group'].invalidate()
        panel(1, name='panel', width=300, height=160)

    for _ in range(6):
        frame(host, body)
    settled = dict(runs)
    assert all(count >= 1 for count in settled.values())
    for _ in range(3):
        frame(host, body)
    assert runs == settled
    # A click on the innermost native view reaches it. The legacy views under
    # the pointer run by the legacy hover rule; the native shell between them does not.
    x, y = seen['leaf cursor']
    click(host, body, (x + 20, y + 10))
    for _ in range(2):
        frame(host, body, (x + 20, y + 10))
    assert seen['clicks'] == 1
    assert runs['shell'] == settled['shell']
    # The legacy group in the middle is invalidated: it runs, its callers do not.
    for _ in range(3):
        frame(host, body)
    settled = dict(runs)
    seen['invalidate'] = True
    frame(host, body)
    assert (runs['panel'], runs['shell'], runs['group']) == (settled['panel'], settled['shell'], settled['group'] + 1)


def test_wrapperless_library_views_take_their_wrapper_in_a_native_view(host):
    """A fast host (fast_view.py) paints into its caller's draw list. A native
    body's is in its own coordinates, so there the call takes the wrapper."""
    from meltygui.view.collection_view import fast_draw_collection
    model = {'enabled': True, 'retries': 3}

    @gui(width=320, height=None)
    def shell(input_value: dict):
        imgui.text('native shell')
        fast_draw_collection(input_value, name='settings')

    def body():
        shell(model, name='shell')

    for _ in range(5):
        frame(host, body)
    islands = {entry.name for entry in host.bridge.entries.values() if entry.site is not None}
    assert 'draw_collection' in islands


def cache_position(host):
    cache = host.bridge.cache
    return cache.position(cache.current)


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


def pressed(input_value, where, shown):
    """A widget body for any host: a button that adds one. `shown` is what it last drew."""
    shown['runs'] = shown.get('runs', 0) + 1
    where['button'] = tuple(imgui.get_cursor_screen_pos())
    press = imgui.button('add one', 100, 24)
    shown['value'] = input_value + press
    return press, input_value + press


def followed(host, body, where, shown):
    """Press the widget once; what it shows right after, and a few frames on."""
    for _ in range(5):
        frame(host, body)
    x, y = where['button']
    click(host, body, (x + 20, y + 10))
    first = shown['value']
    for _ in range(8):
        frame(host, body, (x + 20, y + 10))
    return first, shown['value']


@pytest.mark.parametrize('take', [False, True])
def test_a_native_view_follows_its_legacy_caller_within_a_few_frames(host, take):
    """As on the native host (test_gui_immediate.py): the edit shows at once
    and stays only when the caller passes it back. The cached legacy caller
    is run again to be asked."""
    model, where, shown = {'count': 0}, {}, {}

    @gui(width=200, height=40)
    def stepper(input_value: int):
        return pressed(input_value, where, shown)

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        changed, value = stepper(model['count'], name='stepper')
        if changed and take:
            model['count'] = value
        return False, input_value

    body = lambda: panel(1, name='panel', width=300, height=120)
    assert followed(host, body, where, shown) == (1, int(take))
    runs, (x, y) = shown['runs'], where['button']
    for _ in range(4):
        frame(host, body, (x + 20, y + 10))     # the pointer rests where it pressed
    assert shown['runs'] == runs


@pytest.mark.parametrize('take', [False, True])
@pytest.mark.parametrize('cached', [True, False])
def test_a_legacy_view_follows_its_native_caller_within_a_few_frames(host, take, cached):
    model, where, shown = {'count': 0}, {}, {}

    @render_func(use_cache=cached)
    def child(input_value, draw_state):
        return pressed(input_value, where, shown)

    @gui(width=260, height=120)
    def shell(input_value: dict):
        changed, value = child(input_value['count'], name='child', width=200, height=70)
        if changed and take:
            input_value['count'] = value
            return True, input_value

    assert followed(host, lambda: shell(model, name='shell'), where, shown)[1] == int(take)


@pytest.mark.parametrize('take', [False, True])
@pytest.mark.parametrize('cached', [True, False])
def test_a_legacy_view_follows_its_legacy_caller_on_the_bridge(host, take, cached):
    model, where, shown = {'count': 0}, {}, {}

    @render_func(use_cache=cached)
    def child(input_value, draw_state):
        return pressed(input_value, where, shown)

    @render_func(use_cache=True)
    def panel(input_value, draw_state):
        changed, value = child(model['count'], name='child', width=200, height=70)
        if changed and take:
            model['count'] = value
        return False, input_value

    assert followed(host, lambda: panel(1, name='panel', width=300, height=120), where, shown)[1] == int(take)
