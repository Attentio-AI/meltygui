"""Opening placement must not be inferred from a live resize's zero offset."""
from meltygui.state.new_core_model import ContextMenuWindowState


def test_placement_waits_for_fit_then_runs_once_even_if_open_event_is_replayed():
    state = ContextMenuWindowState()
    assert not state.take_opening_placement(10, fitting=True)
    assert not state.take_opening_placement(10, fitting=True)
    assert state.take_opening_placement(10, fitting=False)
    assert not state.take_opening_placement(10, fitting=False)
    assert not state.take_opening_placement(None, fitting=False)
    # A later reopening of this same state still gets initial placement.
    assert state.take_opening_placement(30, fitting=False)
    assert not state.take_opening_placement(30, fitting=False)


def test_restored_or_direct_menu_gets_only_one_initial_placement():
    state = ContextMenuWindowState()
    state.fit_done = True
    assert state.take_opening_placement(None, fitting=False)
    assert not state.take_opening_placement(None, fitting=False)
