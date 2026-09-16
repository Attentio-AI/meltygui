"""Window gestures share the parameter override path; previews remain transient."""
import libcst as cst
import pytest
from meltygui.state.new_core_model import DrawState
from meltygui.toggles import Toggles
from meltygui.views import anywhere
from meltygui.views.new_core_view import _LazyOverrideEntry
from meltygui.window_visibility import (
    adopt_window_position, marker_user_visibility, sync_marker_visibility,
    requested_window_closed, resolved_window_kwargs, user_window_position,
    user_window_closed, native_user_window_position, override_state,
)
from meltygui.code.libcst_conversion import cst_funcdef_to_dict, dict_to_cst_funcdef


@pytest.fixture
def comment_window(monkeypatch):
    tree = cst_funcdef_to_dict(cst.parse_module('def demo():\n    tensor = make_tensor()\n').body[0])
    entry = _LazyOverrideEntry(tree['locals'], '__tensor__')
    ds = DrawState()
    ds._kwargs = {'preferred_source': 'code comment'}
    srcs = dict(sources={'comment': entry}, kinds={'comment': 'code comment'},
                writable=['comment'], locations={})
    monkeypatch.setattr(anywhere, '_sources_for', lambda *args: srcs)
    monkeypatch.setattr(anywhere, '_input_busy', lambda: False)
    monkeypatch.setattr(anywhere, '_note_scroll', lambda: None)
    monkeypatch.setattr(anywhere, '_anywhere_recompile_tick', lambda *args: None)
    monkeypatch.setattr(anywhere, '_anywhere_verify_tick', lambda *args: None)
    yield ds, tree, entry
    anywhere._DEFERRED_DS.discard(ds)


def test_gestures_round_trip_in_real_comment(comment_window):
    ds, tree, entry = comment_window
    user_window_position(ds, (31.2, -17.9))
    user_window_closed(ds, True)
    source = cst.Module([]).code_for_node(dict_to_cst_funcdef(tree))
    assert 'window_pos=(31, -18)' in source
    assert 'closed=True' in source
    reparsed = cst_funcdef_to_dict(cst.parse_module(source).body[0])
    assert reparsed['locals']['__overrides__']['__tensor__']['closed'] is True
    assert tuple(reparsed['locals']['__overrides__']['__tensor__']['window_pos']) == (31, -18)


def test_drag_defers_source_until_release(comment_window, monkeypatch):
    ds, tree, entry = comment_window
    monkeypatch.setattr(anywhere, '_input_busy', lambda: True)
    for position in [(10, 20), (25, 30), (40, 50)]:
        user_window_position(ds, position)
    assert 'window_pos' not in entry
    assert ds.window_pos == (40, 50)
    monkeypatch.setattr(anywhere, '_input_busy', lambda: False)
    anywhere.flush_deferred_writes()
    assert entry['window_pos'] == (40, 50)
    assert not ds._sa_deferred


def test_source_position_is_adopted_once_and_rescue_is_local():
    ds = DrawState()
    adopt_window_position(ds, {'window_pos': (100, 200)})
    ds.window_pos = (50, 60)  # viewport rescue
    adopt_window_position(ds, {'window_pos': (100, 200)})
    assert ds.window_pos == (50, 60)
    adopt_window_position(ds, {'window_pos': (110, 220)})
    assert ds.window_pos == (110, 220)


def test_saved_closed_allows_preview_without_comment_write(comment_window):
    ds, tree, entry = comment_window
    user_window_closed(ds, True)
    marker = DrawState()
    args = {'closed': True, 'window_pos': (12, 34)}
    sync_marker_visibility(marker, args)
    assert marker._lv_open is False
    assert 'closed' not in args
    assert requested_window_closed(True, {'open_requested': True}) is False
    assert entry['closed'] is True


def test_stale_comment_does_not_cancel_user_reopen(comment_window):
    ds, tree, entry = comment_window
    marker = DrawState()
    marker._lv_window_ds = ds
    sync_marker_visibility(marker, {'closed': True})
    marker._lv_open = True
    marker_user_visibility(marker, False)
    sync_marker_visibility(marker, {'closed': True})
    assert marker._lv_open is True
    assert ds.closed is False


def test_native_gesture_uses_parent_kwargs(comment_window):
    ds, tree, entry = comment_window
    override_state(ds).native_kwargs = ds._kwargs
    ds._kwargs = {'frame_pinned': True}
    native_user_window_position(ds, (25, 70))
    assert entry['window_pos'] == (25, 70)
    assert ds._kwargs == {'frame_pinned': True}


@pytest.mark.parametrize('dangerous', [False, True])
def test_framework_callers_never_receive_automatic_edits(monkeypatch, dangerous):
    monkeypatch.setattr(Toggles, 'dangerous_edit_mode', dangerous)
    srcs = {'kinds': {'caller': 'caller'},
            'locations': {'caller': ('/project/meltygui_pro/editor/text.py', 100)}}
    assert not anywhere.window_source_writable(srcs, 'caller')


def test_callsite_gate_and_comment_movement(monkeypatch):
    srcs = {'kinds': {'caller': 'caller'}, 'locations': {'caller': ('/project/app.py', 1)}}
    monkeypatch.setattr(Toggles, 'dangerous_edit_mode', False)
    assert not anywhere.window_source_writable(srcs, 'caller')
    assert anywhere.window_position_movable(DrawState(),
        {'preferred_source': 'code comment', 'window_pos': (1, 2)})
    monkeypatch.setattr(Toggles, 'dangerous_edit_mode', True)
    assert anywhere.window_source_writable(srcs, 'caller')


def test_removing_closed_override_restores_auto_open():
    marker = DrawState()
    sync_marker_visibility(marker, {'closed': True})
    sync_marker_visibility(marker, {})
    assert marker._lv_open is None


def test_native_request_preserves_user_offset_and_adopts_source(monkeypatch):
    from meltygui.melty import Melty
    monkeypatch.setattr(Melty, 'surface_windows', {})
    monkeypatch.setattr(Melty, 'surface_requests', [])
    ds = DrawState()
    kwargs = {'preferred_source': 'code comment', 'window_pos': (20, 30),
              'window_size': (400, 300), 'open_requested': True}
    req = Melty.surface_window_request('test', 'test', None, kwargs, ds)
    assert not req.pinned
    assert req.window_pos == (20, 30)
    req.window_pos = (90, 100)  # actual compositor move
    Melty.surface_window_request('test', 'test', None, kwargs, ds)
    assert req.window_pos == (90, 100)
    Melty.surface_window_request('test', 'test', None,
                                 kwargs | {'window_pos': (40, 50)}, ds)
    assert req.window_pos == (40, 50)


def test_comment_edit_uses_source_undo_only(comment_window):
    from meltygui.window_visibility import window_edit_is_local
    ds, tree, entry = comment_window
    user_window_closed(ds, True)
    user_window_position(ds, (30, 40))
    assert not window_edit_is_local(ds, 'closed')
    assert not window_edit_is_local(ds, 'window_pos')


def test_native_close_persists_only_user_action(comment_window):
    from types import SimpleNamespace
    from meltygui.app import _note_closed
    ds, tree, entry = comment_window
    req = SimpleNamespace(closed=False, draw_state=ds, surface=None)
    surface = SimpleNamespace(request=req, children=[], stale=False)
    _note_closed(surface)
    assert entry['closed'] is True
    entry.clear()
    _note_closed(surface)  # already closed programmatically
    assert not entry


def test_generic_comment_position_is_movable_without_live_marker(comment_window):
    ds, tree, entry = comment_window
    ds._kwargs = {'window_pos': (20, 30)}
    entry['window_pos'] = (20, 30)
    assert anywhere.window_position_movable(ds, ds._kwargs)


@pytest.fixture
def parameter_panel_sources(monkeypatch):
    """Use real source discovery on a panel nested under a captured value."""
    from meltygui.views import new_core_view as values
    from meltygui.views.new_core_view import ContextMenuState
    from unittest.mock import MagicMock
    empty_host = MagicMock()
    monkeypatch.setattr(values, 'code_hosts_for',
                        lambda *args, **kwargs: (empty_host, empty_host))
    monkeypatch.setattr(anywhere, '_input_busy', lambda: False)
    parent = DrawState()
    parent._kwargs = {'preferred_source': 'code comment'}
    parent.live_root = {'__overrides__': {'__tensor__': {'window_pos': (15, 25)}}}
    parent.live_key = 'tensor'
    parent.closed = False
    panel = DrawState()
    panel._view_func = lambda value: (False, value)
    panel._parent = parent
    panel.parent_window = parent
    panel._kwargs = {}
    panel.closed = False
    srcs = values.collect_input_sources(panel, ContextMenuState())
    monkeypatch.setattr(anywhere, '_sources_for', lambda *args: srcs)
    return parent, panel, srcs


@pytest.mark.parametrize('native', [False, True])
def test_parameter_window_close_does_not_close_inspected_window(parameter_panel_sources, native):
    from meltygui.window_visibility import native_user_window_closed, window_edit_is_local
    parent, panel, srcs = parameter_panel_sources
    assert srcs['comment_owners']['# [tensor]'] is parent
    close = native_user_window_closed if native else user_window_closed
    close(panel, True)
    assert panel.closed is True
    assert parent.closed is False
    assert 'closed' not in parent.live_root['__overrides__']['__tensor__']
    assert window_edit_is_local(panel, 'closed')


def test_parameter_window_move_does_not_move_inspected_window(parameter_panel_sources):
    parent, panel, srcs = parameter_panel_sources
    user_window_position(panel, (90, 120))
    assert panel.window_pos == (90, 120)
    assert parent.live_root['__overrides__']['__tensor__']['window_pos'] == (15, 25)


def test_parameter_edits_still_use_inspected_windows_comment(parameter_panel_sources):
    parent, panel, srcs = parameter_panel_sources
    anywhere.set_anywhere('zoom', 2, panel, allow_any=True, ds_fallback=True)
    assert parent.live_root['__overrides__']['__tensor__']['zoom'] == 2
