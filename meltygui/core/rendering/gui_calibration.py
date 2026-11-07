"""Local runtime costs for the retained GL prototype, independent of cache policy.

Only maintenance() touches the native sampler. Filesystem work runs on a daemon
worker; immutable snapshots contain no environment or persistence machinery.
"""
from dataclasses import dataclass, replace
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from threading import Thread
import hashlib
import json
import math
import os
import platform
import time

VERSION = 1
OPERATIONS = ('allocate', 'transition', 'upload', 'clear', 'resolve', 'composite')
SIZES = (32, 128, 512)
MAX_PROFILE_BYTES = 65536


@dataclass(frozen=True)
class Estimate:
    """Separate clocks in nanoseconds. They must not be added as frame latency."""
    cpu_ns: float
    gpu_ns: float | None
    cpu_measured: bool = False
    gpu_measured: bool = False


@dataclass(frozen=True)
class Terms:
    fixed_ns: float
    per_unit_ns: float
    samples: int = 0

    def estimate(self, units):
        return self.fixed_ns + self.per_unit_ns * max(0, units)


@dataclass(frozen=True)
class OperationCost:
    operation: str
    cpu: Terms
    gpu: Terms


@dataclass(frozen=True)
class CostSnapshot:
    """RGBA8, single-sample costs. Upload units are bytes, other units pixels."""
    operations: tuple[OperationCost, ...]
    revision: int = 0

    def estimate(self, operation, *, pixels=0, bytes=0):
        cost = next(c for c in self.operations if c.operation == operation)
        units = bytes if operation == 'upload' else pixels
        return Estimate(cost.cpu.estimate(units), cost.gpu.estimate(units),
                        cost.cpu.samples > 0, cost.gpu.samples > 0)


# Conservative priors, not measurements or a memory capacity promise.
DEFAULT_COSTS = CostSnapshot(tuple(OperationCost(op, Terms(cpu, slope), Terms(gpu, gpu_slope))
    for op, cpu, slope, gpu, gpu_slope in (
        ('allocate', 50000., .1, 10000., .1),
        ('transition', 5000., 0., 3000., 0.),
        ('upload', 10000., .2, 5000., .1),
        ('clear', 5000., .01, 3000., .05),
        ('resolve', 10000., .01, 5000., .2),
        ('composite', 10000., .01, 5000., .2))))


def _fit(points, clock, previous, *, scaled=True):
    values = [(p[0], p[clock]) for p in points if p[clock] is not None]
    if len(values) < 3:
        return previous
    if not scaled:
        return Terms(sum(y for _, y in values)/len(values), 0., len(values))
    if len({x for x, _ in values}) < 2:
        return previous
    n = len(values)
    mx, my = (sum(p[i] for p in values) / n for i in (0, 1))
    variance = sum((x-mx)**2 for x, _ in values)
    slope = max(0., sum((x-mx)*(y-my) for x, y in values) / variance)
    fixed = my-slope*mx
    if fixed < 0:
        fixed = 0.
        slope = sum(x*y for x,y in values) / sum(x*x for x,_ in values)
    return Terms(fixed, slope, n)


def _profile_path(environment, directory=None):
    key = hashlib.sha256(json.dumps(environment, separators=(',', ':')).encode()).hexdigest()
    root = Path(directory) if directory else Path(os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache') / 'meltygui' / 'gui-costs'
    return root / (key + '.json')


def _load_environment(environment, directory):
    # Bounded local CPU identity read on the file worker, never a hardware scan.
    cpu = []
    try:
        with open('/proc/cpuinfo') as stream:
            for line in stream.read(4096).splitlines():
                key, _, value = line.partition(':')
                if key.strip() in ('vendor_id', 'cpu family', 'model', 'model name', 'stepping'):
                    cpu.append(key.strip() + ':' + value.strip())
                if not line.strip():
                    break
    except OSError:
        pass
    environment = (*environment, platform.node(), platform.release(), '|'.join(cpu))
    path = _profile_path(environment, directory)
    return environment, path, _read_profile(path, environment)


def _read_profile(path, environment):
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_PROFILE_BYTES + 1)
        if len(data) > MAX_PROFILE_BYTES:
            return None
        profile = json.loads(data)
        if profile['version'] != VERSION or profile['environment'] != list(environment):
            return None
        if not 0 <= time.time() - profile['saved_at'] < 30*86400:
            return None
        points = profile['points']
        if set(points) != set(OPERATIONS):
            return None
        for rows in points.values():
            if len(rows) > 24 or not all(_valid(*row) for row in rows):
                return None
        return points
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def _write_profile(path, environment, points):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive temporary names permit independent windows/processes.
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.cost-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(version=VERSION, environment=environment, saved_at=time.time(), points=points), stream)
        temporary.replace(path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _valid(units, cpu, gpu):
    return (isinstance(units, (int, float)) and math.isfinite(units) and 0 <= units <= 2**30
            and isinstance(cpu, (int, float)) and math.isfinite(cpu) and 0 <= cpu < 1e9
            and (gpu is None or isinstance(gpu, (int, float)) and math.isfinite(gpu) and 0 <= gpu < 1e9))


def _background(function, *args):
    result = Future()
    def run():
        try:
            result.set_result(function(*args))
        except Exception as error:
            result.set_exception(error)
    try:
        Thread(target=run, name='gui-cost-profile', daemon=True).start()
    except RuntimeError as error:
        result.set_exception(error)
    return result


class Calibration:
    """One context owner. No GL, disk access or worker creation in __init__."""
    def __init__(self, *, enabled=True, directory=None, clock=time.monotonic):
        self.enabled, self.directory, self.clock = enabled, directory, clock
        self.snapshot = DEFAULT_COSTS
        self.points = {op: deque(maxlen=24) for op in OPERATIONS}
        self.views = {}  # Bounded actual replay estimates, never serialized.
        self.reuses = {}  # Optional observation seam for other rendering policies.
        self.environment = self.path = self.job = None
        self.job_kind = None
        now = clock()
        self.next_tick = self.last_input = now
        self.started = now
        self.last_save = now
        self.refresh_at = now
        self.probe = 0
        self.mismatches = {}
        self.dirty = self.closed = False
        self.presented = False
        self.diagnostics = {'probes': 0, 'samples': 0, 'save_failed': False}

    def input(self):
        self.last_input = self.clock()

    def observe(self, operation, units, cpu_ns, gpu_ns=None, *, view=None):
        """Accept already rendered work; never infer a shader's cost from commands."""
        if not _valid(units, cpu_ns, gpu_ns):
            return
        if view is not None:
            old = self.views.get(view)
            if old is None and len(self.views) >= 256:
                retired = next(iter(self.views))
                self.views.pop(retired)
                self.reuses.pop(retired, None)
            cpu = cpu_ns if old is None else old.cpu_ns*.75 + cpu_ns*.25
            gpu = (gpu_ns if old is None or old.gpu_ns is None else old.gpu_ns
                   if gpu_ns is None else old.gpu_ns*.75 + gpu_ns*.25)
            self.views[view] = Estimate(cpu, gpu, True, gpu is not None)
            return
        if operation not in self.points:
            return
        # Comparable operation costs can suggest device/driver drift. View costs
        # cannot: content or shader changes legitimately alter a view's work.
        cost = next(c for c in self.snapshot.operations if c.operation == operation)
        reference, count = self.mismatches.get(operation, (cost, 0))
        mismatch = any(terms.samples and actual is not None
            and (actual > max(1000., terms.estimate(units))*4
                 or actual*4 < terms.estimate(units))
            for terms, actual in ((reference.cpu, cpu_ns), (reference.gpu, gpu_ns)))
        if mismatch:
            self.mismatches[operation] = (reference, count+1)
            if count+1 >= 8:
                self.probe = 0
                self.refresh_at = self.clock()
                self.mismatches.pop(operation)
        else:
            self.mismatches.pop(operation, None)
        self.points[operation].append((units, cpu_ns, gpu_ns))
        self._update()
        self.dirty = True

    def observe_reuse(self, view):
        """A rendering policy may report a reused view without inventing a GPU time."""
        if view in self.views:
            self.reuses[view] = self.reuses.get(view, 0) + 1

    def _update(self):
        self.snapshot = CostSnapshot(tuple(replace(cost,
            cpu=_fit(self.points[cost.operation], 1, cost.cpu, scaled=cost.operation != 'transition'),
            gpu=_fit(self.points[cost.operation], 2, cost.gpu, scaled=cost.operation != 'transition'))
            for cost in self.snapshot.operations), self.snapshot.revision+1)

    def maintenance(self, native, *, frame_seconds, pressure=False):
        """Call after ordinary rendering, never request a frame to run this.

        At most one probe per 250 ms, after two input-free seconds and a cheap
        frame. Startup and busy frames only use defaults / completed passive data.
        """
        now = self.clock()
        if not self.presented:
            self.presented = True
            return
        if not self.enabled or self.closed or now < self.next_tick or now-self.started < 1.:
            return
        self.next_tick = now + .25
        if self.environment is None:
            self.environment = (*native.calibration_environment(), platform.machine(), platform.python_implementation(), VERSION)
            # Never reuse a different physical GPU's profile when its identity
            # cannot be established. Such contexts still learn in memory.
            if any(part.startswith('uuid:') for part in self.environment if isinstance(part, str)):
                self.job = _background(_load_environment, self.environment, self.directory)
                self.job_kind = 'load' 
        if self.job is not None and self.job.done():
            try:
                result = self.job.result()
            except Exception:
                result = None
            if self.job_kind == 'load' and result:
                self.environment, self.path, result = result
            if self.job_kind == 'load' and result:
                for op, rows in result.items():
                    combined = [*rows, *self.points[op]][-24:]
                    self.points[op].clear()
                    self.points[op].extend(combined)
                self._update()
                self.refresh_at = now + 3600
            elif self.job_kind == 'save':
                self.diagnostics['save_failed'] = not result
                if not result:
                    self.dirty = True
            self.job = None
        for operation, view, units, cpu, gpu in native.calibration_poll():
            self.observe(operation, units, cpu, gpu, view=view if operation == 'replay' else None)
            self.diagnostics['samples'] += 1
        native.calibration_arm()  # One next replay only; native queue is bounded.
        if (not pressure and frame_seconds < .004 and now-self.last_input >= 2.
                and now >= self.refresh_at):
            # Three sizes repeated twice: distinct sizes justify fixed + scaled fits.
            if self.probe >= len(OPERATIONS)*len(SIZES)*2:
                self.probe = 0
                self.refresh_at = now + 3600
            else:
                op = OPERATIONS[self.probe % len(OPERATIONS)]
                size = SIZES[(self.probe // len(OPERATIONS)) % len(SIZES)]
                try:
                    submitted = native.calibration_probe(op, size)
                except RuntimeError:
                    submitted = False
                    self.refresh_at = now + 3600
                    self.diagnostics['probe_failed'] = True
                if submitted:
                    self.probe += 1
                    self.diagnostics['probes'] += 1
        if self.dirty and self.path is not None and self.job is None and now-self.last_save >= 30:
            self._save(now)

    def _save(self, now):
        self.job = _background(_write_profile, self.path, self.environment,
                               {op: list(rows) for op, rows in self.points.items()})
        self.job_kind = 'save'
        self.dirty = False
        self.last_save = now

    def retire(self, view):
        self.views.pop(view, None)
        self.reuses.pop(view, None)

    def close(self):
        # GL queries are owned/deleted by TextureCache.close(), on its context.
        if self.dirty and self.path is not None and (self.job is None or self.job.done()):
            self._save(self.clock())
        self.closed = True
        self.views.clear()
        self.reuses.clear()
