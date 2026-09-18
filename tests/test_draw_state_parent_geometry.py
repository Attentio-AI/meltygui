"""Absolute coordinates must observe parent geometry changes within a frame."""
import pytest

from meltygui.state.new_core_model import Anchor
from meltygui.state.new_core_model import DrawState
from meltygui.state.new_core_model import Pin
from meltygui.core.melty import Melty


@pytest.mark.parametrize('axis', [0, 1])
@pytest.mark.parametrize('parent_anchor', [Anchor.TOP_LEFT, Anchor.CENTER, Anchor.BOTTOM_RIGHT])
def test_cached_nested_position_tracks_same_frame_parent_geometry(monkeypatch, axis, parent_anchor):
    monkeypatch.setattr(Melty, 'frame_count', 500)
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    chain = [DrawState() for _ in range(4)]
    for i, node in enumerate(chain):
        node.width, node.height = 400, 300
        node.left_offset, node.top_offset = 0, 0
        node.window_pos = (10., 20.)
        if i:
            node.parent_window = chain[i - 1]
    child = chain[-1]
    child.parent_anchor_pos = parent_anchor
    read = lambda: child.abs_left if axis == 0 else child.abs_top
    before = read()  # os_frame.solve observes this before the parent's hand edit.
    root = chain[0]
    root.window_pos = (50., 80.)
    assert read() == before + (40 if axis == 0 else 60)
    before = read()
    immediate = chain[-2]
    immediate.width += 100
    immediate.height += 80
    factor = {Anchor.TOP_LEFT: 0, Anchor.CENTER: .5, Anchor.BOTTOM_RIGHT: 1}[parent_anchor]
    assert read() == before + factor * (100 if axis == 0 else 80)
    assert read() == read()  # cached repeat is stable.


def test_absolute_position_parent_cycle_remains_bounded(monkeypatch):
    monkeypatch.setattr(Melty, 'frame_count', 500)
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    first, second = DrawState(), DrawState()
    for node in (first, second):
        node.width, node.height = 200, 100
        node.left_offset, node.top_offset = 0, 0
        node.window_pos = (0., 0.)
    first.parent_window, second.parent_window = second, first
    assert isinstance(first.abs_left, int)
    assert isinstance(first.abs_top, int)


def _nested_chain(monkeypatch, depth=6):
    monkeypatch.setattr(Melty, 'frame_count', 500)
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    chain = [DrawState() for _ in range(depth)]
    for i, node in enumerate(chain):
        node.width, node.height = 400, 300
        node.left_offset, node.top_offset = 5, 7
        node.window_pos = (10., 20.)
        if i:
            node.parent_window = node._parent = chain[i - 1]
    return chain


def test_cached_position_hit_reads_nothing_else(monkeypatch):
    """The regression of 2026-09-16: a hit walked the parent chain. A hit is
    one key comparison at any nesting depth (docs/WINDOW_COLLISION_COLUMNS.md)."""
    chain = _nested_chain(monkeypatch)
    child = chain[-1]
    expected = (child.abs_left, child.abs_top)
    computed = []
    monkeypatch.setattr(DrawState, '_abs_left', lambda self: computed.append(self) or 0)
    monkeypatch.setattr(DrawState, '_abs_top', lambda self: computed.append(self) or 0)
    monkeypatch.setattr(DrawState, '_ancestor_scroll', lambda self: computed.append(self) or (0, 0))
    for _ in range(100):
        assert (child.abs_left, child.abs_top) == expected
    assert computed == []


def test_unchanged_geometry_write_keeps_every_position_cached(monkeypatch):
    chain = _nested_chain(monkeypatch)
    child = chain[-1]
    child.abs_left
    version = Melty.geometry_version
    for node in chain:                      # what the render wrapper does each run
        node.left_offset, node.top_offset = 5, 7
        node.width, node.height = 400, 300
        node.window_pos = (10., 20.)
        node.parent_window = node.parent_window
    assert Melty.geometry_version == version
    chain[0].window_pos = (11., 20.)
    assert Melty.geometry_version == version + 1


@pytest.mark.parametrize('pin', [Pin.PARENT, Pin.WINDOW, Pin.CLIP])
def test_pinned_window_is_cached_and_follows_a_dragged_ancestor(monkeypatch, pin):
    chain = _nested_chain(monkeypatch)
    spawner = chain[-1]
    menu = DrawState()
    menu.width, menu.height = 200, 100
    menu.left_offset, menu.top_offset = 0, 0
    menu.window_pos = (0., 0.)
    menu.parent_window, menu._parent = chain[-2], spawner
    menu.pin_to_clip = pin
    before = (menu.abs_left, menu.abs_top)
    chain[1].window_pos = (110., 70.)       # a window far up the chain is dragged
    assert (menu.abs_left, menu.abs_top) == (before[0] + 100, before[1] + 50)
    walks = []
    original = DrawState._abs_left
    monkeypatch.setattr(DrawState, '_abs_left', lambda self: walks.append(self) or original(self))
    for _ in range(50):
        menu.abs_left
    assert walks == []


def test_state_enum_classification_survives_whole_module_hotswap(monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock
    from meltygui.code.file_converters import _recompile_module

    source = '''from enum import Enum
class Anchor(Enum):
    NEAR = 'near'
    FAR = 'far'
FAR_ANCHORS = (Anchor.FAR,)
def classify(value=Anchor.FAR):
    return value in FAR_ANCHORS
'''
    module = types.ModuleType('_geometry_anchor_hotswap_test')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(Melty, 'cache', MagicMock())
    exec(source, module.__dict__)
    live_anchor, live_classify = module.Anchor.FAR, module.classify
    assert _recompile_module(module, source, '/tmp/geometry_anchor_hotswap_test.py') is None
    assert module.Anchor.FAR is live_anchor
    assert module.FAR_ANCHORS[0] is live_anchor
    assert module.classify is live_classify
    assert live_classify() and live_classify(live_anchor)


def test_hotswap_factory_updates_live_wrapper_and_keeps_closure_state(monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock
    from meltygui.code.file_converters import _recompile_module

    source = '''def make_view():
    state = {'calls': 0}
    def draw_inner_main():
        state['calls'] += 1
        return state['calls'] * 10
    def wrapper():
        return draw_inner_main()
    return wrapper, state
'''
    module = types.ModuleType('_geometry_wrapper_hotswap_test')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(Melty, 'cache', MagicMock())
    exec(source, module.__dict__)
    wrapper, state = module.make_view()
    assert wrapper() == 10
    edited = source.replace('* 10', '* 20')
    assert _recompile_module(module, edited, '/tmp/geometry_wrapper_hotswap_test.py') is None
    assert wrapper() == 40
    assert state == {'calls': 2}

    # A process that already received a factory-only swap can retain wrappers
    # from an older generation; another swap must repair those too.
    orphan, orphan_state = module.make_view()
    assert orphan() == 20
    newer = source.replace('* 10', '* 30')
    scratch = {}
    exec(compile(newer, '/tmp/geometry_wrapper_hotswap_test.py', 'exec'), scratch)
    module.make_view.__code__ = scratch['make_view'].__code__
    assert _recompile_module(module, newer, '/tmp/geometry_wrapper_hotswap_test.py') is None
    assert orphan() == 60
    assert orphan_state == {'calls': 2}
