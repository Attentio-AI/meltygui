"""Bounded raster selection for retained packets, independent of view identity.

The owning runtime supplies only affected replay observations. A retained body
is never executed to measure it or change representation. CPU and GPU clocks
are compared separately; external work is outside this replay decision.
"""
from dataclasses import dataclass


@dataclass
class Candidate:
    refreshes: int = 0
    replays: int = 0
    dirty_fraction: float = 1.
    votes: int = 0
    raster: bool = False


class RasterPolicy:
    def __init__(self):
        self.candidates = {}
        self.promotions = 0
        self.demotions = 0
        self.evaluations = 0
        # Cost terms of the current immutable snapshot, by pixel count and by
        # payload size. Many views share a size, and a recomposed frame
        # evaluates every reused view, so the estimates are computed once.
        self._costs = None
        self._by_pixels = {}
        self._by_bytes = {}

    def retire(self, node):
        self.candidates.pop(node, None)

    def applied(self, node, raster):
        if raster: self.promotions += 1
        else: self.demotions += 1
        if node in self.candidates:
            self.candidates[node].raster = raster

    def choose(self, observation, costs, actual, *, pixels, raster, covered=False):
        """Return a desired representation after two consistent decisions.

        Only actual replays establish reuse. Default cost coefficients are marked
        estimates and require twice the saving margin of calibrated coefficients.
        Fixed shared upload costs are excluded from a child's marginal cost.
        """
        node, refreshes, replays, since_refresh, payload_bytes, commands = observation
        item = self.candidates.get(node)
        if item is None:
            item = self.candidates[node] = Candidate(raster=raster)
        delta = replays - item.replays
        if delta <= 0:
            return raster
        dirty = min(1., max(0., (refreshes-item.refreshes)/delta))
        item.dirty_fraction = .5*item.dirty_fraction + .5*dirty
        item.refreshes, item.replays = refreshes, replays
        item.raster = raster
        self.evaluations += 1
        if not raster and (covered or since_refresh < 2):
            # No reuse opportunity means no possible marginal raster saving.
            # Avoid evaluating six cost terms for constantly changing views.
            item.votes = 0
            return False
        if costs is not self._costs:
            self._costs = costs
            self._by_pixels.clear()
            self._by_bytes.clear()
        terms = self._by_pixels.get(pixels)
        if terms is None:
            if len(self._by_pixels) > 4096: self._by_pixels.clear()
            overhead = [costs.estimate(op, pixels=pixels) for op in
                        ('transition','transition','clear','resolve')]
            terms = self._by_pixels[pixels] = (
                costs.estimate('composite', pixels=pixels), costs.estimate('allocate', pixels=pixels),
                sum(c.cpu_ns for c in overhead), sum(c.gpu_ns for c in overhead))
        composite, allocation, overhead_cpu, overhead_gpu = terms
        # Replay observations time the submitted draw span. Shared buffer uploads
        # occur once outside that span, hence only their marginal byte cost here.
        marginal_upload = self._by_bytes.get(payload_bytes)
        if marginal_upload is None:
            if len(self._by_bytes) > 4096: self._by_bytes.clear()
            marginal_upload = self._by_bytes[payload_bytes] = max(0., (
                costs.estimate('upload', bytes=payload_bytes).cpu_ns-costs.estimate('upload', bytes=0).cpu_ns))
        direct_cpu = marginal_upload + (
            actual.cpu_ns if actual is not None else commands*composite.cpu_ns)
        direct_gpu = actual.gpu_ns if actual is not None else None
        fraction = item.dirty_fraction
        cpu_cached = composite.cpu_ns + fraction*(direct_cpu+overhead_cpu) + allocation.cpu_ns/32
        cpu_margin = .25 if composite.cpu_measured else .5
        cpu_good = cpu_cached < direct_cpu*(1-cpu_margin)
        gpu_good, gpu_bad, cpu_bad = False, False, False
        if direct_gpu is not None:
            gpu_cached = composite.gpu_ns + fraction*(direct_gpu+overhead_gpu) + allocation.gpu_ns/32
            gpu_margin = .25 if composite.gpu_measured else .5
            gpu_good = gpu_cached < direct_gpu*(1-gpu_margin)
            # A regression in the other clock must be small either relative to
            # itself or to the dominant observed work. This permits a few CPU
            # microseconds to save a millisecond shader, but rejects trading it
            # for an expensive CPU capture. Never add the overlapping clocks.
            gpu_bad = gpu_cached > max(direct_gpu*1.25, direct_cpu*.1)
            cpu_bad = cpu_cached > max(direct_cpu*1.25, direct_gpu*.1)
        useful = (not covered and since_refresh >= 2 and (cpu_good or gpu_good)
                  and not gpu_bad and not cpu_bad)
        # Keep a selected cache across modest noise. Constantly dirty content
        # still demotes; a newly cached ancestor makes its descendants redundant.
        if raster and not covered and fraction < .5 and not useful:
            useful = (cpu_cached < direct_cpu*1.1 or gpu_good) and not gpu_bad and not cpu_bad
        direction = 1 if useful else -1
        item.votes = direction if item.votes*direction < 0 else item.votes+direction
        if useful != raster and abs(item.votes) >= 2:
            item.votes = 0
            return useful
        return raster
