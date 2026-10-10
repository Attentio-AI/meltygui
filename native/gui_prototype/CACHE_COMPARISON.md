# Full application cache comparison — October 9, 2026

Latest follow-up: [cache optimization with unchanged per-view granularity](CACHE_OPTIMIZATION.md).
The measurements below describe the earlier baseline.

The approximately 16x speedup is still present with caching disabled. At 300
rows, core render takes **183.06 ms** per redraw and public `@gui` takes
**11.16 ms**. Rebuilding every cached view changes those numbers to
**257.91 ms** and **163.98 ms**, or **1.57x**.

This is not a regression in the fast native wrapper. Enabling the prototype's
retained cache adds a much more expensive execution path: Python orchestration,
per-view ImGui contexts, draw-buffer copying, and individual texture replay.
Even unchanged frames have material Python hit-testing overhead.

## Method

- Current code, after the table collision/rebase fixes, using the public
  decorators and normal application loop. No rendering stages were bypassed.
  Cache-off uses `use_cache=False` on both the root and all workload views.
- Identical collection/button/int/float bodies through `render_func` or `@gui`.
  This is a wrapper/backend comparison, not a claim of production feature parity.
- 25, 100 and 300 rows. Each full redraw executes exactly `4 * rows + 2`
  workload bodies; the outer app root is additional. Counts were verified for
  every measured sample. Unchanged cache-on frames execute zero workload bodies.
- Two independent processes per case, 20 warmup frames and 60 measured frames
  per scenario per process. The second pass reverses case order. Tables pool
  the 120 measured samples per case. Runs were sequential, not competing jobs.
- Native Wayland, RTX 4090, Ryzen 9 7950X, Python 3.12.3, reserved desktop and
  isolated config/cache/state paths. Every framebuffer was 1256 x 1056 pixels
  including the host margin; the requested native window was 1000 x 800.
- Timing runs between event-poll entries and includes the normal surface
  lifecycle, input, collision hooks, rendering, postprocessing, swap, housekeeping
  and GPU completion via `glFinish`. It measures application throughput, not
  physical scanout latency. The display runs at 120 Hz.
- `--viewport` bounds both root collections to the host height. All rows still
  execute; no benchmark culling was added. Without that bound the retained
  auto-height collection reaches the existing 8192-pixel texture-height limit.
  This collection workload does not actively exercise collision gestures.

## Full redraw: cache off versus complete cache invalidation

Median wall milliseconds per frame:

| Rows | Core cache off | Rust cache off | Speedup | Core cache rebuild | Rust cache rebuild | Speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 25 | 17.51 | 8.33 | 2.10x | 25.50 | 15.60 | 1.63x |
| 100 | 62.75 | 8.33 | 7.53x | 87.81 | 55.40 | 1.58x |
| 300 | 183.06 | 11.16 | 16.41x | 257.91 | 163.98 | 1.57x |

The cache-rebuild scenario mutates one field and deliberately invalidates the
**entire** tree each frame. It is a worst-case cache miss, not the cost of a
normal cache hit or a single data edit. Rust performs 103, 403 and 1203 texture
replays respectively. All caches remain enabled during these rebuilds.

Rust's 8.33 ms small-scene results are display-paced. Corresponding cache-off
process CPU medians are 3.22 ms at 25 rows, 5.40 ms at 100, and 11.18 ms at 300.

At 300 rows, enabling and rebuilding the cache adds approximately **152.83 ms**
to Rust versus **74.85 ms** to core render. Thus the prototype's additional cache
work is itself expensive; this is not merely a common fixed cost hiding a faster
wrapper. Wall p95 values at 300 rows are 188.36/12.35 ms with caching off and
268.43/172.02 ms with full cache rebuilds, core/Rust respectively.

## Reuse and selective invalidation

Unchanged cache-on frames:

| Rows | Core wall | Rust wall | Core process CPU | Rust process CPU |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 8.33 ms | 8.33 ms | 2.94 ms | 2.80 ms |
| 100 | 8.33 ms | 8.33 ms | 3.04 ms | 3.81 ms |
| 300 | 8.33 ms | 8.21 ms | 3.69 ms | 8.21 ms |

Both execute zero workload bodies and Rust replays zero textures. Similar wall
times do not mean similar CPU costs: core waits for presentation while the
300-row prototype spends most of that interval working. CPU measurements use
`process_time_ns`, including process threads; they exclude time asleep.

A separate 300-row run mutates `Row 0000.count` and invalidates only that row's
object through each framework's object-invalidation API. After 20 warmup frames,
40 measured frames give:

| Implementation | Median wall | Median process CPU | Workload bodies/frame | Rust texture replays/frame |
| --- | ---: | ---: | ---: | ---: |
| Core, cache on | 246.05 ms | 249.61 ms | 1155 | — |
| Rust, cache on | 15.28 ms | 15.28 ms | 2 | 4 |

Independent retained invalidation delivers **16.1x** here: the row collection
and its changed integer execute, followed by the necessary containing texture
replays. Core's current object-invalidation path executes most of this scene.
These are observed body counts, not an assumption that both cache algorithms
do equivalent work.

Nevertheless, Rust's 15.28 ms sparse-update frame is still slower than its
11.16 ms cache-off full redraw in this particular scene. The retained model is
saving executions, but the current bookkeeping consumes those savings.

## Where the time goes

Separate cProfile runs retain the same rendering stages. They inflate Python
execution time, so their timings are diagnostic averages, not substitutes for
the unprofiled medians above. Inclusive timings overlap and must not be added.

At 300 rows, a full Rust cache rebuild has:

| Operation | Calls/frame | Diagnostic time/frame |
| --- | ---: | ---: |
| Native `TextureCache.render` | 1203 | 65.79 ms |
| Native `TextureCache.add_packet` | 1203 | 10.12 ms |
| Native `RetainedGraph.info` | 18637 | 11.24 ms |
| Python retained `_execute`, exclusive | 1203 | 18.66 ms |
| Input application to private ImGui contexts, inclusive | 1203 | 10.97 ms |
| ImGui `new_frame` | 1204 | 4.56 ms |

The entire profiled frame averages 234.12 ms, versus the unprofiled 163.98 ms
median. Native texture replay includes driver work and waits; it is not a GPU
timer query. The benchmark's final `glFinish` is only the remaining GPU wait,
not a measurement of all GPU work.

The code explains the replay cost: each cached node has its own target/context;
each replay saves/restores GL state, uploads buffers, rasterizes commands and
runs a separate alpha-resolve pass. The shared scratch FBO is deleted/recreated
when the next view has a different size. Those are real operations even though
the loop issuing them is Rust. This run did not isolate the scratch allocation
cost from the other replay work.

The unchanged-frame profile identifies another specific problem:
`ImGuiInput.hit` scans the retained nodes and walks their ancestors every frame.
It accounts for 7.45 ms inclusive in that diagnostic, with **6914**
`RetainedGraph.info` calls per frame across the frame. `info` builds Python
dictionaries instead of performing a narrow native geometry query.

With one row invalidated, additional retained pointer/clip checks and cleared
position caches raise this to **15036 info calls/frame** (8.73 ms in the
diagnostic). Only four texture replays remain, taking **0.51 ms** in that run.
This explains why sparse updates are still expensive despite executing only
two bodies: they are dominated by scanning and graph-to-Python conversion.

## Recommended next experiments

1. Fix hit-testing first: avoid whole-tree rescans when neither pointer nor
   relevant geometry changed, and perform geometry/ancestry queries in Rust
   without constructing thousands of Python snapshots. Invalidation must still
   cover geometry changes, retirement and z order; stationary input alone is
   not enough to assume the hit target is unchanged.
2. Reuse scratch targets and batch texture replay under a shared GL state guard.
   Measure each change without dropping rendering stages or changing fidelity.
3. Reconsider one texture and one ImGui context per small widget. Preserve
   per-view identity, dependencies, returned values and independent invalidation,
   but experiment with shared capture contexts and grouping draw packets into
   fewer raster targets. Cache granularity need not equal function granularity.
4. Move remaining retained orchestration into Rust after identifying the work
   that should remain. A language port alone will not remove 1203 GPU replays.

The fast native wrapper remains promising. The full-miss texture pipeline and
the cache-hit input path are two separate problems with measured targets.
These are proposed experiments, not measured optimization gains.

## Reproduction and data

```bash
.venv/bin/python tests/benchmark_gui_full_app.py --variant rust --cache off --rows 300 --viewport --warm 20 --frames 60 --out /tmp/rust-off.json
.venv/bin/python tests/benchmark_gui_full_app.py --variant rust --cache on --rows 300 --viewport --warm 20 --frames 60 --out /tmp/rust-on.json
.venv/bin/python tests/benchmark_gui_full_app.py --variant python --cache on --rows 300 --viewport --warm 20 --frames 60 --out /tmp/core-on.json
.venv/bin/python tests/benchmark_gui_full_app.py --variant rust --cache on --rows 300 --viewport --scenarios row --warm 20 --frames 40 --out /tmp/rust-row.json
.venv/bin/python tests/benchmark_gui_full_app.py --variant rust --cache on --rows 300 --viewport --scenarios redraw --warm 20 --frames 40 --profile-phase redraw --profile /tmp/rust-redraw.pstats --out /tmp/rust-profile.json
```

Run in a reserved desktop session. Switch `--variant` and `--cache` for the
remaining combinations. `--profile-phase hot` or `row` selects the other paths.

[Raw samples, diagnostic results and per-function profiles](cache_comparison_results_2026-10-09.json)
contain all 24 primary runs and the separate diagnostics.
