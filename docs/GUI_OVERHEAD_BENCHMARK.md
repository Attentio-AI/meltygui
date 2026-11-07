# Plain Python versus public `@gui`: measured overhead

This is the recorded per-view raster baseline. The current default includes
[automatic raster selection](GUI_AUTO_CACHE.md); use `Toggles.Rust.auto_cache = False`
for that representation baseline. See the newer report for matched current
raw/disabled/default/calibrated comparisons rather than mixing run timings.

Measured 2026-11-07 with [benchmark_gui_overhead.py](../tests/benchmark_gui_overhead.py).
The [follow-up profiling report](GUI_OVERHEAD_PROFILING.md) attributes the costs
and identifies the repeated eviction scan behind much of the hot graphics overhead.
This compares **the same recursive body** dispatched directly, through public
`@gui(use_cache=False)`, and through public cached `@gui`. It does not compare
against the older `@render_func` wrapper. No production rendering code was changed.

The uncached decorator adds measurable overhead. Retained capture/redraw adds
considerably more. Culling wins when the skipped work pays for that overhead;
it loses for sufficiently cheap bodies. Reusing unchanged captures is a separate
optimization, and its frame time is not a per-call overhead measurement.

## Machine and measurement

- AMD Ryzen 9 7950X, 16 cores / 32 threads; CPython 3.12.3; Linux 6.19.7.
- OpenGL renderer: NVIDIA GeForce RTX 4090/PCIe/SSE2; NVIDIA driver 595.99.02.
  The machine has two RTX 4090s; this uses one ordinary GLFW OpenGL context.
- CPU matrix: 16 warmup frames, 80 recorded frames per variant and case.
  GL matrix: 12 warmup frames, 60 recorded frames. Variant order is shuffled
  deterministically each frame. GC stays enabled; scenes are collected between cases.
- Runs were sequential, on the active workstation without changing CPU/GPU clocks,
  process affinity or other users' processes. They are local observations, not
  machine-independent constants. p95 and median absolute deviation (MAD) are
  retained in the JSON, together with every recorded sample.
- GPU work ran through `agent-desk-2`, in hidden GLFW windows, with separate
  XDG config/state/cache directories. No user input or existing window was used.

CPU-only timings include the public wrapper, native frame scope, identities,
recursive traversal, retained host-frame reconciliation, invalidation, and flush.
The minimal dispatch test excludes frame setup and has no traversal or useful body
work. GL CPU timings additionally include capture, composition, host presentation,
ImGui render and renderer submission. Common outer ImGui frame/window setup,
event polling, swapping and display presentation are excluded.

GPU numbers are `GL_TIME_ELAPSED` intervals, not a measure of GPU utilization.
Query completion waits are outside the CPU submission timer. The JSON also has
wall time through query completion. Timer quantization and driver submission
affect very short intervals. Benchmark-only synchronization does not model
steady-state CPU/GPU overlap.

## Workload and correctness

Trees have explicit stable keys and deterministic node numbers. Depth includes
the root: depth 5, fanout 4 contains 341 nodes. Each body accumulates a count and
checksum, optionally performs 64 or 512 iterations of a deterministic 32-bit
integer recurrence, and calls the same child-dispatch expression. GL adds one
colored rectangle per executed body. Raw and decorated bodies preserve the same
input object and return `(False, input_value)`.

All-visible trees partition a 128-by-512 root vertically; every node intersects
its ancestors' clip. There is no occlusion culling. Heavy-culling trees place
siblings one viewport apart, leaving only the first branch visible. The raw
baseline still traverses every logical node; `raw_manual_cull` separately adds
the same hierarchical visibility decision before entering an invisible body.

Two culling setups are measured:

- Lazy mount: 341 logical nodes, 5 visible bodies, 17 retained declarations.
  Invisible branches have not yet declared their own descendants.
- Already mounted (`--predeclare`): all 341 retained declarations are created
  before moving siblings out of view. All remain declared during measurements.
  Only 5 bodies execute when dirty. This includes the cost of invalidating the
  full retained graph, rather than treating it as a 17-node graph.

`gui_redraw` invalidates the body function before **every** frame, marking all
associated retained declarations dirty. That cost is included and also recorded
separately. `gui_hot` makes no mutation or invalidation. There are zero model
mutations in all variants: forced invalidation isolates full-redraw overhead
without introducing different data-copy/update algorithms into the baseline.

Every measured frame asserts body count, checksum, result and input identity.
Native uncached calls must equal logical node count. Retained IDs must be unique,
declaration counts must match the mount mode, and native visible counts must
match the independently computed visible set. GL mount output is compared
byte-for-byte against raw output for **every** variant/case, after settling the
host window. All 14 final GL cases passed. Raster-pass deltas are recorded:
341 for all-visible forced redraw, 5 for culled forced redraw, 0 for hot frames.

## CPU results

A separate 1,000-call loop with only `return False, input_value` measured
**0.031 us/raw call versus 1.532 us/public uncached call**, an added **1.50 us**
per call. This is a minimal dispatch lower bound, not a nested rendering result.

All-visible trivial bodies, median frame time in microseconds:

| Depth | Fanout | Nodes | Raw | Uncached `@gui` | Retained redraw | Retained hot | Added uncached / node | Added retained redraw / node |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 1 | 0.40 | 5.47 | 19.27 | 9.27 | 5.07 | 18.87 |
| 8 | 1 | 8 | 1.74 | 26.52 | 128.18 | 9.69 | 3.10 | 15.81 |
| 32 | 1 | 32 | 6.55 | 111.39 | 579.91 | 13.41 | 3.28 | 17.92 |
| 5 | 4 | 341 | 65.44 | 1,060.23 | 5,679.55 | 14.86 | 2.92 | 16.46 |
| 4 | 8 | 585 | 114.89 | 2,086.59 | 10,748.33 | 20.03 | 3.37 | 18.18 |

Added cost is `(decorated median - raw median) / executed bodies`, and is only
shown for equal-work cases. Nested uncached cost is approximately linear over
these sizes, around 3 us added per node. Retained full redraw costs roughly
16–18 us extra per node before graphics. The single-node case includes a much
larger share of fixed frame costs. No hot-frame time is divided by logical nodes.

For the 341-node all-visible tree, median / p95 in milliseconds:

| Body work | Raw | Uncached `@gui` | Retained redraw | Retained hot |
| --- | ---: | ---: | ---: | ---: |
| Trivial | 0.065 / 0.077 | 1.060 / 1.215 | 5.680 / 6.350 | 0.015 / 0.021 |
| 64 iterations | 1.105 / 1.177 | 2.273 / 2.432 | 7.622 / 8.709 | 0.022 / 0.032 |
| 512 iterations | 8.966 / 9.137 | 9.966 / 10.158 | 15.277 / 15.567 | 0.026 / 0.045 |

Uncached/raw ratios are **16.2x, 2.06x and 1.11x** as useful body work increases.
Retained full-redraw/raw ratios are **86.8x, 6.89x and 1.70x**. Hot frames execute
zero bodies; their large gains on expensive bodies are avoided work, not cheaper
dispatch. For trivial 341-node frames, CPU MAD is 1.3 us raw, 24.6 us uncached,
166.1 us retained redraw and 1.6 us retained hot.

Already-mounted heavy culling, 341 declared nodes and 5 visible bodies,
median / p95 in microseconds:

| Body work | Raw full traversal | Raw manual culling | Retained redraw | Retained hot |
| --- | ---: | ---: | ---: | ---: |
| Trivial | 63.6 / 101.6 | 3.56 / 5.87 | 186.3 / 299.2 | 14.7 / 37.3 |
| 64 iterations | 1,098.3 / 1,175.8 | 19.0 / 22.4 | 204.5 / 263.1 | 15.4 / 27.3 |
| 512 iterations | 8,831.8 / 8,998.3 | 132.4 / 136.1 | 332.0 / 426.6 | 18.1 / 39.9 |

Dirty culling is **2.93x slower** than full raw traversal for trivial bodies,
then **5.37x and 26.6x faster** for the two heavier bodies. It remains slower than
manual raw culling: the latter provides neither framework state/invalidation nor
retained rendering resources. Full-graph invalidation alone takes 8.8, 8.9 and
14.9 us median in these three rows. Lazy culling's trivial dirty frame is 170.9 us;
it is useful, but using it alone would hide the cost of retaining 341 declarations.

A local linear estimate from the trivial already-mounted case puts dirty-culling
break-even near **0.37 us of additional useful work per logical body**:
`(186.3 - 63.6) / (341 - 5)`. That is roughly 0.55 us total raw work per node
including this tree's trivial traversal/body cost. This is an estimate, not a
measured universal threshold. The measured crossover lies between 0 and 64
iterations; timer noise, object access, layout and different visibility ratios
can move it substantially.

## Actual OpenGL rendering

Same 341-node tree and identical final pixels. CPU submission median / p95 in
milliseconds; GPU interval median is in microseconds. Culling rows use the
already-mounted graph.

| Visibility and work | Variant | Bodies | CPU median / p95 | GPU interval |
| --- | --- | ---: | ---: | ---: |
| All visible, trivial | Raw | 341 | 0.288 / 0.374 | 3.1 |
| All visible, trivial | Uncached `@gui` | 341 | 2.012 / 2.321 | 3.1 |
| All visible, trivial | Retained redraw | 341 | 19.034 / 22.383 | 5,270.0 |
| All visible, trivial | Retained hot | 0 | 0.489 / 0.664 | 4.1 |
| All visible, 512 iterations | Raw | 341 | 9.815 / 12.119 | 6.1 |
| All visible, 512 iterations | Uncached `@gui` | 341 | 11.382 / 13.828 | 6.1 |
| All visible, 512 iterations | Retained redraw | 341 | 29.091 / 33.137 | 5,731.8 |
| All visible, 512 iterations | Retained hot | 0 | 0.520 / 0.639 | 5.1 |
| Culled, trivial | Raw | 341 | 0.233 / 0.282 | 2.0 |
| Culled, trivial | Raw manual culling | 5 | 0.082 / 0.130 | 2.0 |
| Culled, trivial | Retained redraw | 5 | 0.544 / 0.807 | 38.4 |
| Culled, trivial | Retained hot | 0 | 0.123 / 0.149 | 3.1 |
| Culled, 512 iterations | Raw | 341 | 9.618 / 10.407 | 3.1 |
| Culled, 512 iterations | Raw manual culling | 5 | 0.230 / 0.265 | 2.0 |
| Culled, 512 iterations | Retained redraw | 5 | 0.868 / 1.149 | 39.9 |
| Culled, 512 iterations | Retained hot | 0 | 0.149 / 0.196 | 3.1 |

The full-redraw graphics path costs **66.1x raw** for trivial bodies and **2.96x**
for heavier bodies. Even the all-visible hot path is **1.70x slower than raw**
for trivial rectangles: skipping bodies does not eliminate retained presentation
and geometry work. Heavy already-mounted culling wins **11.1x over full raw** in
the 512-iteration GL case, but manual raw culling is still 3.78x faster than
retained redraw.

For trivial all-visible GL frames, wall time through GPU-query completion is
0.312 ms raw, 2.042 ms uncached, 19.352 ms retained redraw and 0.511 ms hot.
Initial mount is separate: roughly 0.5 ms raw, 2.6 ms uncached and 72–74 ms
retained. Mounting every node before culling takes about 83–86 ms for the
retained trivial case, versus about 2.4–2.5 ms for lazy culling. These are
single mount observations, not warmed distributions.

This exercises GLFW/OpenGL with small rectangle draw lists, not interactive
widgets, event routing, OS presentation, native Wayland hosting, CUDA tensors,
large images or live editor workload. It measures the opt-in prototype's
current tradeoffs rather than claiming all GUI workloads behave alike.

## Reproduce and inspect

The four final reports are bundled as `gui-overhead-2026-11-07.json`, delivered
through Hub Files. Each case stores counts, checksums, mount time, median, p95,
MAD, minimum, maximum, raw samples, dirty counts and, for GL, raster passes,
GPU intervals, completion times and cache resource statistics.

```sh
.venv/bin/python tests/benchmark_gui_overhead.py \
  --trees 1:1,8:1,32:1,5:4,4:8 --work 0,64,512 \
  --frames 80 --warm 16 --json-out /tmp/gui-overhead-cpu.json

.venv/bin/python tests/benchmark_gui_overhead.py \
  --trees 5:4,4:8 --work 0,64,512 --visibility culled --predeclare \
  --frames 80 --warm 16 --json-out /tmp/gui-overhead-cpu-predeclared.json

# Run these through launch on a reserved agent seat, with isolated XDG paths.
.venv/bin/python tests/benchmark_gui_overhead.py --graphics \
  --trees 8:1,4:4,5:4 --work 0,512 \
  --frames 60 --warm 12 --json-out /tmp/gui-overhead-gl.json

.venv/bin/python tests/benchmark_gui_overhead.py --graphics \
  --trees 5:4 --work 0,512 --visibility culled --predeclare \
  --frames 60 --warm 12 --json-out /tmp/gui-overhead-gl-predeclared.json
```

The script uses public `meltygui.gui` with the private window adapter to isolate
the requested costs. It does not enter the complete OS-window application loop.
No native extension rebuild is necessary if the prototype is already built.
All rendering, capture and timer-query work stays on the context-owning thread.

## Automatic selection follow-up

[Automatic raster selection](GUI_AUTO_CACHE.md) now includes matched
legacy/default/calibrated measurements, retained shared replay, actual workload
observations, native Wayland verification and residual CPU profiling. The earlier
tables above remain historical measurements, not the baseline for the new ratios.
