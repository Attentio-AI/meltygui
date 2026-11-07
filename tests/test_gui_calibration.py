"""Calibration policy is testable without a GL context, a worker or a wall clock."""
from concurrent.futures import Future
import json
import math
import time

import pytest
from meltygui.core.rendering import gui_calibration as calibration
from meltygui.core.rendering.gui_calibration import Calibration, DEFAULT_COSTS, OPERATIONS


class Clock:
    def __init__(self): self.now = 0.
    def __call__(self): return self.now


class Native:
    def __init__(self):
        self.calls = []
        self.observations = []
    def calibration_environment(self):
        self.calls.append('environment')
        return ('vendor', 'gpu', 'driver', 'glsl', 'uuid:1234', 'rgba8')
    def calibration_poll(self):
        self.calls.append('poll')
        result, self.observations = self.observations, []
        return result
    def calibration_arm(self): self.calls.append('arm')
    def calibration_probe(self, op, size):
        self.calls.append((op, size))
        return True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def immediate(function, *args):
        future = Future()
        future.set_result(function(*args))
        return future
    monkeypatch.setattr(calibration, '_background', immediate)
    clock, native = Clock(), Native()
    owner = Calibration(directory=tmp_path, clock=clock)
    return owner, native, clock


def tick(rig, now, **kwargs):
    owner, native, clock = rig
    clock.now = now
    owner.maintenance(native, frame_seconds=kwargs.pop('frame_seconds', .001), **kwargs)


def test_first_usable_frame_never_loads_or_calibrates_even_after_slow_launch(rig):
    owner, native, _ = rig
    tick(rig, 10)
    assert not native.calls
    assert owner.snapshot is DEFAULT_COSTS and owner.job is None


def test_defaults_and_incomplete_async_load_do_not_wait(rig, monkeypatch):
    owner, native, _ = rig
    pending = Future()
    monkeypatch.setattr(calibration, '_background', lambda *args: pending)
    tick(rig, 0)
    tick(rig, 1)
    tick(rig, 2)
    assert owner.snapshot is DEFAULT_COSTS
    assert not pending.done()
    assert owner.diagnostics['probes'] == 1
    assert owner.environment is not None


def test_idle_budget_input_interruption_and_no_catchup_loop(rig):
    owner, native, clock = rig
    tick(rig, 0)
    tick(rig, 1)
    assert owner.diagnostics['probes'] == 0
    tick(rig, 2, pressure=True)
    tick(rig, 3, frame_seconds=.010)
    assert owner.diagnostics['probes'] == 0
    clock.now = 4
    owner.input()
    tick(rig, 5)
    tick(rig, 6)
    assert owner.diagnostics['probes'] == 1
    tick(rig, 6.1)
    assert owner.diagnostics['probes'] == 1
    tick(rig, 100)
    assert owner.diagnostics['probes'] == 2  # No loop catching up missed ticks.
    for index in range(40): tick(rig, 101 + index)
    assert owner.diagnostics['probes'] == 36
    assert owner.refresh_at > clock.now


def test_separate_cpu_gpu_fits_immutable_snapshot_and_byte_units():
    owner = Calibration()
    previous = owner.snapshot
    for units in (100, 200, 100, 200):
        owner.observe('upload', units, 500 + 2*units, 3000 + 5*units)
    estimate = owner.snapshot.estimate('upload', bytes=400, pixels=99999)
    assert estimate.cpu_ns == pytest.approx(1300)
    assert estimate.gpu_ns == pytest.approx(5000)
    assert estimate.cpu_measured and estimate.gpu_measured
    assert not previous.estimate('upload').cpu_measured
    assert previous is DEFAULT_COSTS
    assert previous.estimate('upload', bytes=400) != estimate
    for units in (100, 200, 100): owner.observe('clear', units, 10+units)
    clear = next(op for op in owner.snapshot.operations if op.operation == 'clear')
    assert clear.cpu.samples == 3 and clear.gpu.samples == 0
    assert clear.gpu == next(op.gpu for op in DEFAULT_COSTS.operations if op.operation == 'clear')


def test_invalid_and_single_size_samples_do_not_make_a_scaled_fit():
    owner = Calibration()
    for _ in range(10): owner.observe('clear', 100, 10, 20)
    assert next(op for op in owner.snapshot.operations if op.operation == 'clear').cpu.samples == 0
    before = owner.snapshot
    for cpu, gpu in ((math.nan, 1), (1, math.inf), (-1, 1), (1, 1e12)):
        owner.observe('clear', 200, cpu, gpu)
    assert owner.snapshot is before


def test_bounded_view_observation_reuse_retirement_and_content_changes(rig):
    owner, _, clock = rig
    owner.observe('replay', 10, 100, 100, view=1)
    owner.observe_reuse(1)
    assert owner.reuses[1] == 1
    owner.refresh_at = 9999
    for _ in range(7): owner.observe('replay', 10, 100, 1000, view=1)
    assert owner.refresh_at == 9999
    owner.observe('replay', 10, 100, 1000, view=1)
    assert owner.refresh_at == 9999 and owner.views[1].gpu_ns > 100
    prior_gpu = owner.views[1].gpu_ns
    owner.observe('replay', 10, 200, None, view=1)
    assert owner.views[1].gpu_ns == prior_gpu
    for view in range(2, 300): owner.observe('replay', 1, 1, 2, view=view)
    assert len(owner.views) == 256 and 1 not in owner.reuses
    owner.retire(299)
    assert 299 not in owner.views
    assert all(not rows for rows in owner.points.values())


def test_profile_missing_corrupt_version_environment_and_staleness(tmp_path):
    environment = ('uuid:123', 'driver-v1')
    path = calibration._profile_path(environment, tmp_path)
    points = {op: [(100, 1000, 3000), (200, 1100, None)] for op in OPERATIONS}
    assert calibration._read_profile(path, environment) is None
    path.write_text('{broken')
    assert calibration._read_profile(path, environment) is None
    assert calibration._write_profile(path, environment, points)
    assert calibration._read_profile(path, environment) is not None
    assert calibration._read_profile(path, ('uuid:other', 'driver-v1')) is None
    assert calibration._profile_path(('uuid:other', 'driver-v1'), tmp_path) != path
    profile = json.loads(path.read_text())
    for changes in ({'version': 999}, {'saved_at': time.time()-31*86400}, {'points': {}}):
        path.write_text(json.dumps({**profile, **changes}))
        assert calibration._read_profile(path, environment) is None
    path.write_text(' ' * (calibration.MAX_PROFILE_BYTES+1))
    assert calibration._read_profile(path, environment) is None
    path.write_text('[]')
    assert calibration._read_profile(path, environment) is None


def test_persistence_failure_and_closed_owner_do_not_affect_rendering(rig, monkeypatch):
    owner, native, _ = rig
    tick(rig, 0)
    tick(rig, 1)
    monkeypatch.setattr(calibration, '_write_profile', lambda *args: False)
    owner.observe('clear', 100, 200, 300)
    tick(rig, 31)
    tick(rig, 32)
    assert owner.diagnostics['save_failed']
    owner.close()
    before = list(native.calls)
    tick(rig, 100)
    assert native.calls == before


def test_unidentifiable_device_keeps_models_local(rig):
    owner, native, _ = rig
    native.calibration_environment = lambda: ('vendor', 'gpu', 'unidentified-device')
    tick(rig, 0)
    tick(rig, 1)
    tick(rig, 2)
    assert owner.path is None and owner.job is None
    assert owner.diagnostics['probes'] == 1


def test_unavailable_gpu_results_do_not_change_estimates(rig):
    owner, native, _ = rig
    tick(rig, 0)
    tick(rig, 1)
    tick(rig, 2)
    assert owner.snapshot is DEFAULT_COSTS
    native.observations = [('replay', 99, 100, 500, 2000)]
    tick(rig, 3)
    assert owner.views[99].cpu_ns == 500 and owner.views[99].gpu_ns == 2000


def test_atomic_writer_keeps_old_profile_when_replace_fails(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / 'profile.json'
    path.write_text('old profile')
    def fail(*args): raise PermissionError('read-only cache')
    monkeypatch.setattr(Path, 'replace', fail)
    assert not calibration._write_profile(path, (), {})
    assert path.read_text() == 'old profile'
    assert list(tmp_path.iterdir()) == [path]


def test_sustained_comparable_operation_mismatch_requests_idle_refresh(rig):
    owner, _, clock = rig
    for units in (100,200,100,200):
        owner.observe('clear',units,1000+units,2000+units)
    owner.refresh_at = 9999
    for _ in range(7): owner.observe('clear',100,1000,30000)
    assert owner.refresh_at == 9999
    owner.observe('clear',100,1000,30000)
    assert owner.refresh_at == clock.now


def test_transition_is_fixed_and_failed_probe_backs_off(rig):
    owner,native,_ = rig
    for units in (100,200,300): owner.observe('transition',units,1000,2000)
    assert owner.snapshot.estimate('transition',pixels=100) == owner.snapshot.estimate('transition',pixels=1000000)
    def fail(*args): raise RuntimeError('context resource unavailable')
    native.calibration_probe=fail
    tick(rig,0)
    tick(rig,2)
    assert owner.diagnostics['probe_failed'] and owner.refresh_at==3602


def test_empty_xdg_cache_home_uses_home_cache(monkeypatch,tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME','')
    monkeypatch.setattr(calibration.Path,'home',lambda:tmp_path)
    assert calibration._profile_path(('device',)).parent == tmp_path/'.cache'/'meltygui'/'gui-costs'


def test_saved_model_restores_without_waiting_or_probing(rig):
    owner,native,clock = rig
    tick(rig,0)
    tick(rig,1)
    points = {op:[(100,1200,2000),(200,1400,3000),(100,1200,2000)] for op in OPERATIONS}
    assert calibration._write_profile(owner.path,owner.environment,points)
    restored = Calibration(directory=owner.directory,clock=clock)
    restored.maintenance(native,frame_seconds=.001)
    clock.now = 3
    restored.maintenance(native,frame_seconds=.001)
    estimate = restored.snapshot.estimate('clear',pixels=300)
    assert estimate.cpu_ns == pytest.approx(1600)
    assert estimate.gpu_ns == pytest.approx(4000)
    assert restored.diagnostics['probes'] == 0


def test_late_profile_load_preserves_newest_samples(rig,monkeypatch,tmp_path):
    owner,_,_ = rig
    pending = Future()
    monkeypatch.setattr(calibration,'_background',lambda *args:pending)
    tick(rig,0)
    tick(rig,1)
    recent = [(100,5000,7000),(200,6000,8000),(100,5000,7000)]
    for row in recent: owner.observe('clear',*row)
    saved = {op:[(100,1000,2000),(200,2000,3000)]*12 for op in OPERATIONS}
    pending.set_result((owner.environment,tmp_path/'profile',saved))
    tick(rig,2)
    assert list(owner.points['clear'])[-3:] == recent
    assert len(owner.points['clear']) == 24


def test_disabled_calibration_never_touches_native(rig):
    owner,native,_ = rig
    owner.enabled=False
    for now in (0,1,2,10000):tick(rig,now)
    assert not native.calls and owner.snapshot is DEFAULT_COSTS
