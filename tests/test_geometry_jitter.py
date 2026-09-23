"""Rendered geometry diagnostics: catch transient faults without moving anything."""
from types import SimpleNamespace
import weakref

import pytest

from meltygui.core.diagnostics import geometry_jitter as jitter
from meltygui.core.melty import Melty


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(pointer=[100.0, 100.0], origin=[0.0, 0.0],
                            down=True, surface=None, records=[])
    state.view = SimpleNamespace(id="column", name="column", parent_window=None,
                                 width=200.0, height=150.0, abs_left=10.0, abs_top=20.0,
                                 frame_count=10, closed=False, expanded=True)
    monkeypatch.setattr(jitter, "_STUDIO", {})
    monkeypatch.setattr(jitter, "_SURFACES", weakref.WeakKeyDictionary())
    monkeypatch.setattr(jitter, "_ERRORS", set())
    monkeypatch.setattr(jitter.edge_motion_guard, "_enabled", lambda: True)
    monkeypatch.setattr(jitter.edge_motion_guard, "_button_down", lambda: state.down)
    monkeypatch.setattr(jitter.edge_motion_guard, "_surface", lambda: state.surface)
    monkeypatch.setattr(jitter.edge_motion_guard, "_origin", lambda: tuple(state.origin))
    monkeypatch.setattr(jitter.edge_motion_guard, "_pointer", lambda origin: tuple(state.pointer))
    monkeypatch.setattr(Melty, "event_handler", SimpleNamespace(
        is_down=lambda button: state.down and button == "right_mouse"))
    monkeypatch.setattr(Melty, "frame_count", 100)
    monkeypatch.setattr(jitter.resize_trace, "record",
                        lambda stage, window, **details: state.records.append((stage, details)))

    def frame(sample=True, dx=0, dy=0):
        Melty.frame_count += 1
        state.pointer[0] += dx
        state.pointer[1] += dy
        if sample:
            jitter.sample(state.view)
        jitter.check_frame()

    state.frame = frame
    state.reports = lambda: [entry for stage, entry in state.records if stage == "draw-state-jitter"]
    return state


@pytest.mark.parametrize("field", jitter.FIELDS)
def test_stationary_pointer_catches_each_geometry_field_and_snapback(rig, field):
    original = getattr(rig.view, field)
    rig.frame()
    setattr(rig.view, field, original + 15)
    rig.frame()
    setattr(rig.view, field, original)
    rig.frame()
    report = rig.reports()[-1]
    change = report["candidates"][0]["changes"][0]
    assert change["field"] == field
    assert "one-frame reversal without pointer reversal" in change["kinds"]
    assert [frame["views"][0]["geometry"][field] for frame in report["samples"]] == [
        original, original + 15, original]
    assert any(stage == "draw-state-jitter-followup" for stage, _ in rig.records)
    assert getattr(rig.view, field) == original


def test_reversal_inside_pointer_budget_still_reports(rig):
    rig.frame()
    rig.view.width += 6
    rig.frame(dx=10)
    assert not rig.reports()
    rig.view.width -= 6
    rig.frame(dx=10)
    assert rig.reports()[0]["candidates"][0]["changes"][0]["kinds"] == [
        "one-frame reversal without pointer reversal"]


def test_smooth_move_resize_contact_and_real_pointer_reversal_are_quiet(rig):
    rig.frame()
    for delta in (8, 8, 0, 0, -8, -8):
        rig.view.abs_left += delta
        rig.view.abs_top += delta
        rig.view.width += 2 * delta
        rig.view.height += 2 * delta
        rig.frame(dx=delta, dy=delta)
    assert not rig.records


def test_rounding_is_quiet(rig):
    rig.frame()
    for delta in (1, -1, 2, -2):
        rig.view.width += delta
        rig.frame()
    assert not rig.records


@pytest.mark.parametrize("release", [False, True])
def test_smooth_one_frame_lag_at_pointer_stop_is_quiet(rig, release):
    rig.frame()
    rig.frame(dx=6)
    rig.view.abs_left += 6
    rig.frame(dx=6)
    rig.view.abs_left += 6
    rig.down = not release
    rig.frame()
    assert not rig.records
    # A subsequent unprompted step has no unconsumed hand motion.
    rig.view.abs_left += 6
    rig.frame()
    assert len(rig.reports()) == 1


def test_native_origin_shift_is_in_budget_and_trace(rig):
    rig.frame()
    rig.origin[0] += 20
    rig.view.abs_left -= 20
    rig.frame()
    assert not rig.reports()
    rig.view.abs_left -= 50
    rig.frame()
    report = rig.reports()[0]
    assert report["samples"][-1]["origin"] == (20, 0)


def test_release_frame_snapback_and_followups_are_captured(rig):
    rig.frame()
    rig.view.abs_top += 30
    rig.frame()
    rig.down = False
    rig.view.abs_top -= 30
    rig.frame()
    assert rig.reports()[-1]["samples"][-1]["buttons"] == []
    rig.frame()
    count = len(rig.records)
    rig.view.abs_top += 90
    rig.frame()
    assert len(rig.records) == count
    assert "history" not in jitter._STUDIO


def test_missing_samples_and_new_gestures_do_not_compare_stale_geometry(rig):
    rig.frame()
    rig.frame(sample=False)
    rig.view.width += 80
    rig.frame()
    assert not rig.reports()
    rig.down = False
    rig.frame()
    rig.down = True
    rig.view.width += 80
    rig.frame()
    assert not rig.reports()


def test_surface_histories_are_separate_despite_process_wide_frame_numbers(rig):
    class Surface:
        title = "test surface"

    first, second = Surface(), Surface()
    rig.surface = first
    rig.frame()
    rig.surface = second
    rig.view.width += 100
    rig.frame()
    assert not rig.reports()
    rig.surface = first
    rig.frame()
    assert len(rig.reports()) == 1
    assert rig.reports()[0]["samples"][0]["frame"] == 101
    assert rig.reports()[0]["samples"][-1]["frame"] == 103
    rig.surface = None
    del first
    assert len(jitter._SURFACES) == 1


def test_bounded_reports_history_and_no_duplicate_end_frame(rig):
    rig.frame()
    for _ in range(40):
        rig.view.width += 50
        rig.frame()
        jitter.check_frame()
    assert len(rig.reports()) == jitter.MAX_REPORTS_PER_GESTURE
    assert len(jitter._STUDIO["history"]) == jitter.HISTORY_FRAMES
    assert not jitter._STUDIO["followups"]


def test_disabled_diagnostics_do_not_read_geometry(rig, monkeypatch):
    monkeypatch.setattr(jitter.edge_motion_guard, "_enabled", lambda: False)
    rig.view = object()
    rig.frame()
    assert not rig.records


def test_failed_geometry_read_is_visible_once_and_does_not_break_frame(rig):
    rig.view.width = None
    rig.frame()
    rig.frame()
    assert [stage for stage, _ in rig.records] == ["draw-state-jitter-error"]


def test_nonfinite_geometry_is_reported(rig):
    rig.frame()
    rig.view.height = float("nan")
    rig.frame()
    assert rig.reports()[0]["candidates"][0]["changes"][0]["kinds"] == ["non-finite geometry"]


def test_wrapper_samples_are_copied_and_identify_the_renderer(rig):
    def render_column(input_value):
        return False, input_value

    Melty.frame_count += 1
    jitter.sample(rig.view, render_column)
    # A later parent solve must not overwrite what this wrapper rendered.
    rig.view.width = 250
    jitter.check_frame()
    rig.frame()
    report = rig.reports()[0]
    assert report["samples"][0]["views"][0]["geometry"]["width"] == 200
    assert report["samples"][0]["views"][0]["renderer"].endswith("render_column")


def test_hotswap_module_execution_retains_surface_and_studio_history(rig):
    from pathlib import Path

    rig.frame()
    namespace = {"__name__": jitter.__name__, "__package__": jitter.__package__,
                 "_STUDIO": jitter._STUDIO, "_SURFACES": jitter._SURFACES,
                 "_ERRORS": jitter._ERRORS}
    exec(compile(Path(jitter.__file__).read_text(), jitter.__file__, "exec"), namespace)
    assert namespace["_STUDIO"] is jitter._STUDIO
    assert namespace["_SURFACES"] is jitter._SURFACES
    assert namespace["_STUDIO"]["history"][0]["samples"][id(rig.view)]["geometry"][0] == 200
