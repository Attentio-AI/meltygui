"""Native state uses the app's object serializer, exclusions and pruning."""
import gc
import weakref

import pytest

from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from meltygui.core.runtime.app_session import AppSession, prune
from native_gui_support import native_frame


class ViewSettings(DictConversion):
    def __init__(self):
        super().__init__()
        self.selection = []


def restore(session):
    result = loads(dumps(session), run_on_load=False)
    prune(result)
    return result


def make_view(cache, default=2):
    @cache.gui(level=default, no_save=('scratch',), inject={'local': list})
    def view(input_value, draw_state=None, level=1, scratch='fresh', local=None, cache=None):
        if input_value == 'edit':
            draw_state.level = 9
            draw_state.scratch = 'temporary'
            local.append('saved')
        return False, (level, scratch, local, draw_state)
    return view


def test_object_roundtrip_owned_state_exclusions_and_default_precedence():
    session = AppSession()
    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    view = make_view(cache)
    with cache.frame():
        _, (_, _, local, state) = view('edit', arbitrary_caller_kwarg=object())
    assert 'arbitrary_caller_kwarg' not in state.as_dict()
    assert state.level == 9 and state.dlt_count == 1
    cache_ref = weakref.ref(cache)
    cache.close()
    assert 'cache' not in state.as_dict()
    del view, cache
    gc.collect()
    assert cache_ref() is None

    session = restore(session)
    restored = session.draw_state_registry[state.unique]
    assert restored.level == 9 and restored.dlt_count == 0
    assert not {'scratch', 'cache', 'abs_left', 'abs_top', 'depth'} & restored.as_dict().keys()
    assert restored.local == ['saved']

    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    view = make_view(cache, default=7)
    with cache.frame():
        _, (level, scratch, local, state2) = view(None)
    assert level == 9 and scratch == 'fresh' and local == ['saved']
    assert state2 is restored
    with cache.frame():
        assert view(None, level=4)[1][0] == 4
    with cache.frame():
        assert view(None)[1][0] == 9
    del state2.level
    with cache.frame():
        view(0)
    assert state2.level == 7
    cache.close()
    session = restore(session)
    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    with cache.frame():
        assert make_view(cache, default=12)(None)[1][0] == 12
    cache.close()


def test_cached_descendants_stay_live_and_hidden_views_are_pruned():
    session = AppSession()
    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    immediate = _new_native(graphics=False, identities=cache.identities)
    seen, calls = {}, []

    @_bind_native(immediate)
    def uncached(input_value, draw_state=None):
        seen['uncached'] = draw_state.unique

    @cache.gui
    def leaf(input_value, draw_state=None):
        calls.append('leaf')
        seen['leaf'] = draw_state.unique
        uncached(None)

    @cache.gui
    def root(input_value, draw_state=None):
        calls.append('root')
        seen['root'] = draw_state.unique
        if input_value: leaf(None)

    # Skip both cached bodies for long enough to catch frame-age eviction bugs.
    for _ in range(250):
        with native_frame(immediate):
            with cache.frame(): root(True)
    assert calls == ['root', 'leaf']
    cache.prepare_save()
    assert set(restore(session).draw_state_registry) == set(seen.values())
    with native_frame(immediate):
        with cache.frame(): root(False)
    cache.prepare_save()
    assert set(restore(session).draw_state_registry) == {seen['root']}
    cache.close()
    immediate.clear()
    assert set(restore(session).draw_state_registry) == {seen['root']}


def test_loaded_but_unused_state_expires_and_sibling_windows_are_independent():
    session = AppSession()
    first = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    second = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    first.identities.set_scope('first')
    second.identities.set_scope('second')
    with first.frame(): a = make_view(first)('edit')[1][-1].unique
    with second.frame(): b = make_view(second)('edit')[1][-1].unique
    first.close()
    second.close()
    session = restore(session)
    assert set(session.draw_state_registry) == {a, b}
    # A restored object not used at all has a zero countdown and is dropped.
    assert restore(session).draw_state_registry == {}


def test_native_state_serialization_preserves_cycles_and_caller_exclusions():
    cache = RetainedGui(graphics=False)
    @cache.gui
    def view(input_value, draw_state=None, keep=1, drop=2, loop=None):
        draw_state.loop = draw_state
        return False, draw_state
    with cache.frame(): state = view(None)[1]
    restored = loads(dumps(state, excluded=['drop']), run_on_load=False)
    assert restored.loop is restored
    assert restored.keep == 1 and 'drop' not in restored.as_dict()
    cache.close()
    del restored, state, view, cache
    gc.collect()


def test_app_session_disk_roundtrip_restores_annotated_state(tmp_path, monkeypatch):
    from meltygui.core.runtime import app_session
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    session = AppSession()

    def bind(cache):
        @cache.gui
        def view(input_value, settings: ViewSettings = None):
            if input_value: settings.selection.append(input_value)
            return False, settings
        return view

    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    with cache.frame(): bind(cache)('chosen')
    cache.close()
    assert app_session.save(session, 'native-test') is not None
    loaded = app_session.load('native-test')
    cache = RetainedGui(graphics=False, state_registry=loaded.draw_state_registry)
    with cache.frame(): settings = bind(cache)(None)[1]
    assert isinstance(settings, ViewSettings)
    assert settings.selection == ['chosen']
    cache.close()


def test_named_window_geometry_survives_restart_and_initial_does_not_pin():
    session = AppSession()

    def bind(cache):
        @cache.gui(width=300, height=100)
        def view(input_value, draw_state=None):
            return False, draw_state
        @cache.gui
        def root(input_value):
            view({'hello', 1}, name='view1', melty_window=True,
                 initial={'width': 200, 'height': 90, 'left': 10, 'top': 20})
            view({'hello', 1}, name='view2', melty_window=True)
        return root, view

    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    root, _ = bind(cache)
    with cache.frame(): root(None)
    windows = tuple(cache.graph.windows())
    rectangles = ((55.25, 80.5, 420.75, 180.25), (510., 160., 350., 220.))
    for node, rect in zip(windows, rectangles):
        cache.graph.allocate(node, rect)
        cache.flush()
    # Checkpoint without closing, then exercise another frame before close.
    cache.prepare_save()
    checkpoint = restore(session)
    with cache.frame(): root(1)
    assert tuple(cache.graph.info(n)['rect'] for n in windows) == rectangles
    cache.close()

    for loaded in (checkpoint, restore(session)):
        cache = RetainedGui(graphics=False, state_registry=loaded.draw_state_registry)
        root, _ = bind(cache)
        with cache.frame(): root(None)
        assert tuple(cache.graph.info(n)['rect'] for n in cache.graph.windows()) == rectangles
        cache.close()


def test_explicit_window_geometry_wins_and_geometry_can_be_excluded():
    session = AppSession()
    def bind(cache, no_save=()):
        @cache.gui(width=300, height=100, no_save=no_save)
        def view(input_value): pass
        return view
    cache = RetainedGui(graphics=False, state_registry=session.draw_state_registry)
    view = bind(cache, ('left', 'top', 'width', 'height'))
    with cache.frame(): view(None, melty_window=True)
    node = next(iter(cache.graph.windows()))
    cache.graph.allocate(node, (80, 90, 500, 250))
    cache.flush()
    cache.close()
    loaded = restore(session)
    state = next(iter(loaded.draw_state_registry.values()))
    assert not {'left', 'top', 'width', 'height'} & state.as_dict().keys()

    cache = RetainedGui(graphics=False, state_registry=loaded.draw_state_registry)
    view = bind(cache)
    with cache.frame(): view(None, melty_window=True, width=450, height=200, left=60, top=70)
    node = next(iter(cache.graph.windows()))
    assert cache.graph.info(node)['rect'] == (60, 70, 450, 200)
    cache.close()
    loaded = restore(loaded)
    cache = RetainedGui(graphics=False, state_registry=loaded.draw_state_registry)
    view = bind(cache)
    with cache.frame(): view(None, melty_window=True, width=275, height=125, left=25, top=35)
    node = next(iter(cache.graph.windows()))
    assert cache.graph.info(node)['rect'] == (25, 35, 275, 125)
    cache.close()
