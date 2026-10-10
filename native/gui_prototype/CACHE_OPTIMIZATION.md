# Cache optimization without changing view granularity (2026-10-09)

This follow-up keeps a separate retained node, ImGui context, command packet and
output texture for every cached view. Small widgets still have caching enabled.
The public decorators, dynamic dependency injection, returned-value replay,
explicit invalidation, layout/collision work and full host rendering path remain
in use. The earlier measurements are in [CACHE_COMPARISON.md](CACHE_COMPARISON.md).

## Changes

### Native hit query

`RetainedGraph.hits` resolves portal occlusion, absolute positions, ancestor clips
and node order in Rust. Both the ImGui input bridge and retained hit regions use
it. It returns only hit candidates and their view origins, rather than exporting
thousands of graph dictionaries into Python. Portal title bars still occlude
underlying controls, portals escape their ancestors' clips, and inline children
respect those clips. It queries current geometry on every invocation, so motion,
resize, z-order changes and retirement do not require a new invalidation scheme.
Absolute positions use double precision to match the previous Python sums of
single-precision stored geometry; logical layout coordinates are not rounded.

### Reusable temporary framebuffers

Texture replay previously deleted and reallocated its single accumulation target
whenever the next view had a different size. An LRU pool now holds up to eight
exact-size temporary targets. These are scratch storage, not cached view outputs.
They count against the existing texture budget and are evicted under pressure.
Allocating/resizing an output also reserves enough budget for the largest replay.
No output texture is evicted, its name remains stable across resize, and cleanup
releases the entire pool. `scratch_stats()` exposes count, bytes and allocations
for diagnostics. Alpha accumulation and the straight-alpha resolve are unchanged.

### One GL state guard per composition batch

`render_many` replays the same bottom-up list under one host GL state guard,
replacing a complete save/restore around every view. Each view still uploads its
packets, rasterizes its own texture and runs its alpha-resolve pass. Exceptions
restore host state and leave nodes pending for a later composition attempt.

## Measurement method

The unchanged `tests/benchmark_gui_full_app.py` runs public `@gui`/`@os_window`
and core render through the normal application loop. It measures input, layout,
collision hooks, draw submission, GPU completion (`glFinish`), presentation and
housekeeping. No workload or stage is omitted. Each case gets two fresh processes,
20 warmup frames and 60 measured frames per scenario. The second run reverses
case order. Runs are sequential, on a reserved native Wayland desktop with
isolated application state, RTX 4090 and Ryzen 9 7950X. The host framebuffer is
1256 x 1056, including its margin. The viewport bounds the root texture height;
all rows still execute.

An unchanged cached frame executes zero workload bodies and zero texture replays.
A complete invalidation executes `4 * rows + 2` workload bodies plus the app root;
Rust replays `4 * rows + 3` separate output textures. Sample counts and framebuffer
sizes are checked before aggregating results. Single-row invalidation is measured
separately. CPU time uses `process_time_ns`; wall time on fast frames is capped by
the 120 Hz display. cProfile results are separate diagnostic runs and are not used
for the headline medians.

## Results

Median full-invalidation wall milliseconds per frame (120 samples per case):

| Rows | Core cache on, fresh run | Rust cache on, before | Rust cache on, now | Current speedup vs core |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 25.03 | 15.60 | 10.36 | 2.42x |
| 100 | 88.19 | 55.40 | 35.31 | 2.50x |
| 300 | 257.09 | 163.98 | 102.66 | 2.50x |

The 300-row rebuild is **37.4% faster than the previous Rust cache path**.
It still executes 1202 workload bodies plus the root and replays all 1203 textures.

Unchanged-frame process CPU milliseconds (wall medians remain about 8.33 ms):

| Rows | Core, fresh run | Rust before | Rust now |
| ---: | ---: | ---: | ---: |
| 25 | 2.97 | 2.80 | 2.32 |
| 100 | 3.06 | 3.81 | 2.42 |
| 300 | 3.59 | 8.21 | 2.80 |

Rust now uses less CPU than core in all three unchanged-scene cases.

Fresh cache-off full-redraw results (median wall / process CPU ms):

| Rows | Core | Rust |
| ---: | ---: | ---: |
| 25 | 17.31 / 17.36 | 8.33 / 3.30 |
| 100 | 61.81 / 61.88 | 8.33 / 5.40 |
| 300 | 179.36 / 179.54 | 10.92 / 10.97 |

At 300 rows the uncached speedup remains **16.42x**. Cache rebuild adds about
**91.74 ms** to Rust versus **77.74 ms** to core, comparing wall medians. Thus
overall cached frames beat core, but the incremental cost of a complete cache
miss is still higher than core’s incremental cache cost. This distinction matters:
we have not recovered the uncached 16x advantage for full cache rebuilds.

Selective row invalidation, 300 rows (20 warmup, 40 measured frames):

| Variant | Wall ms | Process CPU ms | Workload bodies | Texture replays |
| --- | ---: | ---: | ---: | ---: |
| python | 246.95 | 249.78 | 1155 | — |
| rust | 8.31 | 2.93 | 2 | 4 |

Previously the Rust row-update frame took 15.28 ms wall/CPU. Its new CPU cost
is 2.93 ms and presentation limits wall time to about 8.33 ms. The unchanged
body/replay counts demonstrate that this gain is bookkeeping and replay overhead,
not a change to invalidation granularity.

## Diagnostic profiles

| Operation | Before | After |
| --- | ---: | ---: |
| Full rebuild: texture replay | 65.79 ms (1203 guarded calls) | 14.98 ms (one batch, 1203 replays) |
| Full rebuild: graph dictionary snapshots | 18637/frame | 3610/frame |
| Unchanged: ImGui hit query | 7.45 ms | 0.19 ms |
| Unchanged: graph dictionary snapshots | 6914/frame | 2/frame |

These are cProfile diagnostic averages; profiling inflates Python execution.
Do not add inclusive times or compare them directly to unprofiled wall medians.
The remaining profiled capture path still executes 1203 bodies/contexts. Its
inclusive `_execute` time is 141.39 ms, versus 139.99 ms previously, while replay
falls sharply. Draw-packet ingestion alone takes 9.26 ms in the new profile.
Native replay includes GL driver work/waits; this is not an isolated GPU timer.

[Raw samples and profiles](cache_optimization_results_2026-10-09.json)

Reproduction uses the commands in [the baseline report](CACHE_COMPARISON.md#reproduction-and-data).

## Verification and limits

The existing 229 tests passed after the optimization. Additional regression tests
compare the Rust query with the original clipping/ownership rules across geometry,
z-order and lifetime changes; check texture pixels across alternating scratch
sizes; exercise budget eviction and reuse; and verify GL state restoration after
both successful and failed batches. Existing GPU tests cover fractional positions,
alpha, nested composition and independently refreshed children. Table/column,
window drag/resize and ImGui widget input tests remain enabled.

Final focused suite: **232 passed**. Live `example_ui.py` checks on the reserved
Wayland desktop covered nested title movement, nested column-divider movement,
nested right-drag resize and root right-drag resize. The root changed from
2627 x 1524 to 2577 x 1484 for a (-50, -40) drag; the nested divider changed
adjacent column widths from 217/217 to 257/177. The app remained responsive
with no crash or new traceback.

The complete cache-miss path still captures every Python view into its own ImGui
context, marshals draw buffers and commands, uploads geometry, rasterizes and
resolves alpha for each output. These optimizations remove avoidable overhead;
they do not make those operations free or establish a 16x full-cache speedup.
Cache granularity is deliberately deferred. Platform measurements and live input
checks here cover Linux Wayland/OpenGL, not macOS or Windows.
