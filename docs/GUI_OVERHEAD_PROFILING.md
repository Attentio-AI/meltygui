# Where the `@gui` overhead goes

This profiles the earlier per-view raster path. Subsequent
[small optimizations](GUI_OPTIMIZATION_EXPERIMENTS.md) and
[automatic shared replay](GUI_AUTO_CACHE.md) changed the runtime. The latter
report includes residual profiling of the current automatic path.

Profiled 2026-11-07, following the [matched workload benchmark](GUI_OVERHEAD_BENCHMARK.md).
The principal scene is the same depth-5, fanout-4 tree: 341 nodes, trivial bodies,
one small rectangle per body for OpenGL. No production runtime optimization or
extension replacement was made.

The overhead has three distinct sources:

1. **Uncached dispatch:** argument dictionaries, geometry/state-field preparation
   and parameter resolution dominate the native wrapper. Identity hashing/lookup
   is a smaller component. Public Python dispatch adds another measurable layer.
2. **Retained redraw:** declarations and execution bookkeeping repeatedly cross
   Python/Rust boundaries, configure state, check arguments, register aliases,
   and run per-view layout/capture lifecycles. Tiny bodies do too little useful
   work to amortize this machinery.
3. **Graphics:** every dirty view builds an ImGui frame and command packet, then
   the retained renderer uploads and composes it. Separately, an unnecessary
   steady-state eviction scan explains most of the surprising hot-cache cost.

The eviction scan is a removable prototype bookkeeping issue, **not an intrinsic
cost of retained rendering**. Per-view capture and composition are costs of the
current implementation and granularity, not proof that another implementation
must pay the same amount.

## The hot-cache finding and causal check

[`RetainedGui._evict_scroll_tiles`](../meltygui/core/rendering/retained_gui_prototype.py)
enters its loop whenever there are more than 256 scroll tiles or more than 32 MiB
of captured tiles. In the all-visible scene there are 340 tiles, all visible,
occupying only 1 MiB. No tile can be evicted, but every flush scans them again.
The harness calls flush twice, causing **680 `graph.visible` calls per frame**.

| Same scene, production extension | Median CPU | p95 CPU | GPU interval | Resident resources |
| --- | ---: | ---: | ---: | --- |
| Normal 256-tile limit | 332.0 us | 545.5 us | 3.1 us | 341 contexts, 340 tiles, 1 MiB |
| Benchmark-only limit of 1,000,000 | 94.9 us | 102.0 us | 3.1 us | 341 contexts, 340 tiles, 1 MiB |

Only the benchmark instance's tile limit changes, after mounting. Pixels and
resource counts match. The change saves **237.1 us, 71.4% of the median frame**,
or a 3.50x speedup. It does not remove a capture, draw, or body execution: both
cases execute zero bodies. Raising this limit globally is not proposed as the
fix; eviction remains necessary when content becomes invisible or exceeds its
budget. A next experiment should schedule eviction eligibility work when its
inputs change rather than repeatedly scanning an unchanged, unevictable set.

The coarse phase profile independently attributes 228.1 us of its 343.7 us mean
frame to eviction (66.4%). Host submission consumes 80.9 us (23.5%), retained
declaration 15.5 us (4.5%), and the other phases/residual consume 19.2 us.
Native probes time the 680 visibility checks at 175.3 us; the rest of the scan
is Python loop/method dispatch and associated bookkeeping. These are different
runs and instrumentation layers, so their numbers should not be added together.

## Method and instrumentation cost

Hardware remains Ryzen 9 7950X, CPython 3.12.3, NVIDIA RTX 4090/OpenGL on the same
Linux workstation. The profiler harness runs 20 warmups and 80–200 measured
frames per case, sequentially. This differs from the earlier benchmark's
interleaved variant order; use the new baseline below when comparing profiles.
Clocks, affinity and other workstation processes were not changed.

Tools and scope:

- [`profile_gui_overhead.py`](../tests/profile_gui_overhead.py) reuses the exact
  benchmark `Scene`, body and data. Body counts, checksums and results are
  checked every frame. The final graphics attribution/ablation runs also verify
  framebuffer equality with a raw-function reference.
- `cProfile` supplies call counts and Python/exposed-C-call attribution. It
  cannot see inside `Renderer.__call__`; that native aggregate appears within
  the Python `_bind_native` wrapper's self time. Its rankings and counts are
  useful, but its heavy timing perturbation prevents treating its milliseconds
  as ordinary execution costs.
- Nested phase timers wrap methods in the profiling process. A process-local
  AST copy of `_execute` adds four scopes: ImGui setup, ImGui finalize, packet
  capture and capture metadata. Production source files are not changed.
- [`build_gui_profile_native.py`](../tests/build_gui_profile_native.py) copies
  the Rust crate into `/tmp/gui-profile/native-detailed`, adds thread-local
  counters and `Instant` intervals, and builds into that copy's own target
  directory. The harness explicitly loads that library. The normal extension
  remains unchanged. Native timings include internal Python API/driver calls
  inside each measured Rust section; they are not pure Rust instruction time.
- The installed `perf` launcher lacked a binary for the custom running kernel.
  No attachment to a live application or machine-wide profiling setting was
  attempted. Native attribution therefore uses explicit source probes rather
  than inferring Rust internals from Python stacks.

Instrumentation changes timings. Representative **median** frame times:

| Case | Ordinary | cProfile | Nested phase timers | Isolated native timers |
| --- | ---: | ---: | ---: | ---: |
| CPU uncached | 0.975 ms | 1.283 ms | 1.148 ms | 1.103 ms |
| CPU retained redraw | 5.964 ms | 13.873 ms | 7.405 ms | 6.743 ms |
| GL retained redraw | 17.665 ms | 32.058 ms | 22.347 ms | 18.497 ms |
| GL retained unchanged | 0.330 ms | 0.539 ms | 0.339 ms | 0.348 ms |

Phase tables below use **mean exclusive phase time / mean frame time from the
same instrumented run**. They are not normalized to make the uninstrumented
frame total fit. Native subcomponents are marked as such; do not add them to
their enclosing Python or native phase. The total native wrapper probe excludes
the complete Python body interval, including descendants. Existing `body_us`
values are never summed across retained runtimes: their child-runtime spans
would be counted repeatedly.

## Uncached dispatch: what the native aggregate contains

For 341 bodies, the isolated native profile has a mean frame of **1,103.8 us**.
The native function's complete time outside its Python body totals **730.9 us**
(66.2% of that frame, 2.14 us/body). The following are nonoverlapping sections
within that native function:

| Native section | Mean us/frame | us/body | Share of instrumented frame |
| --- | ---: | ---: | ---: |
| Copy/merge argument dictionaries and reserved arguments | 67.3 | 0.197 | 6.1% |
| Resolve identity, find state, fetch runtime context | 110.3 | 0.323 | 10.0% |
| Resolve geometry, write framework state fields, prepare events | 229.7 | 0.674 | 20.8% |
| Resolve signature parameters, build callback keyword dict, update slots | 171.1 | 0.502 | 15.5% |
| Enter identity/body frame | 13.3 | 0.039 | 1.2% |
| Post-body cleanup/stat bookkeeping | 18.9 | 0.056 | 1.7% |
| Prepare return | 15.6 | 0.046 | 1.4% |
| Native lifetime/timer bookkeeping outside those sections | 104.7 | 0.307 | 9.5% |
| Outside the measured native function, including useful body | 372.9 | 1.093 | 33.8% |

The geometry/event section has **no delivered events in this workload**; its
cost is geometry resolution and ordinary framework-field preparation. Identity
`resolve` itself takes 42.5 us, including 28.7 us in `handle`; both are nested
inside the 110.3-us row. These are warm identity hits, not repeated cold SHA-256
identity generation. Source: `Renderer.__call__` in
[`native/gui_prototype/src/lib.rs`](../native/gui_prototype/src/lib.rs), and
[`identity.rs`](../native/gui_prototype/src/identity.rs).

Controlled dispatch-layer measurements use the production extension, the same
body, tree, keys, state and native frame, after mounting:

| Dispatch path | Median us/frame | us/body |
| --- | ---: | ---: |
| Public `@gui` | 976.2 | 2.863 |
| Bound Python wrapper, bypassing public window lookup/dispatch | 858.5 | 2.518 |
| Native `Renderer` callable, also bypassing Python hotswap check | 830.5 | 2.436 |

The first layer costs about **117.7 us/frame (0.345 us/body)**; the next costs
about **28.0 us/frame (0.082 us/body)**. Bypassing them is an attribution
experiment, not an optimization recommendation: those paths intentionally omit
public dispatch and/or live-code checking. Sources:
[`gui_prototype.gui`, `_bind_native`](../meltygui/core/rendering/gui_prototype.py)
and [`_GuiWindow.call`](../meltygui/core/rendering/gui_window_prototype.py).

The remaining cost includes useful Python work, keyword construction/copies,
Python calls, the PyO3 ingress/egress conversion and object destruction. This
experiment does **not** separately resolve every nanosecond of allocator versus
reference-count versus ABI cost. In particular, the outside-native remainder
must not all be labeled "Python/Rust boundary overhead."

## Retained CPU redraw: the larger bookkeeping layer

With nested phase timers the all-visible CPU frame averages **7,479.6 us**;
ordinary frames measure 5,964.0 us median. Exclusive phase results:

| Phase | Mean us/frame | us/executed body | Share |
| --- | ---: | ---: | ---: |
| Retained declaration wrapper, excluding child phases | 2,792.9 | 8.19 | 37.3% |
| `_execute` bookkeeping, excluding child phases | 2,186.1 | 6.41 | 29.2% |
| Native dispatch and body, excluding separately timed descendants | 1,631.2 | 4.78 | 21.8% |
| Layout commit | 479.1 | 1.40 | 6.4% |
| Layout begin | 252.8 | 0.74 | 3.4% |
| Retirement, layout captured, invalidation, flush and other phases | 123.8 | 0.36 | 1.7% |
| Unassigned harness/timer boundary remainder | 13.8 | 0.04 | 0.2% |

The native-dispatch phase includes local Python body/callback overhead and some
profiling overhead; it is not the same quantity as native-only wrapper time.

The declaration wrapper in
[`retained_gui_prototype.py`](../meltygui/core/rendering/retained_gui_prototype.py)
does identity lookup, caller/default/initial argument assembly, state
configuration, bounds checks, graph declaration, argument-change comparison,
invocation copies, aliases and geometry binding. `_execute` manages graph/frame
lifecycle, result/state association, measurement, background metadata, commits
and cleanup. These run for every dirty body even when the body only increments
a counter and draws a rectangle.

`cProfile` counts **5,115 dict pops**, **3,751 `math.isfinite` calls**, **682
`any` calls**, and **683 alias constructions** per 341-node redraw. Literal dict
construction and interpreter keyword allocation do not have independent
profiler events, so those counts are not an allocation census.

Low-overhead native probes provide additional detail inside those larger phases
(different instrumented run, 6,705.2 us mean frame):

| Native operation | Calls/frame | Mean us/frame | us/body |
| --- | ---: | ---: | ---: |
| `NativeState.configure` | 341 | 447.3 | 1.31 |
| All native renderer work outside Python bodies | 341 | 944.7 | 2.77 |
| Graph declaration | 341 | 118.9 | 0.35 |
| Graph alias association | 682 | 94.6 | 0.28 |
| Graph commit | 341 | 82.8 | 0.24 |
| Visibility test before child execution | 340 | 132.2 | 0.39 |
| Identity handle | 341 | 53.6 | 0.16 |
| Full graph invalidation | 1 | 15.0 | 0.04 |
| `EdgeGraph.fork` | 682 | 14.4 | 0.04 |

These are subsets of the previous table, not additional costs. Dirty-node
scheduling itself is tiny in this scenario; recursive declarations execute the
already-dirty children before the final flush has much scheduling left to do.

**Layout snapshot qualification:** `GuiGeometry.begin` forks two edge graphs and
copies layout/frame/binding/window maps for each execution. Here all those maps
are empty. The 682 native forks cost only 14.4 us total, so a costly populated
constraint graph is **not** responsible for this scene's milliseconds. The
Python begin/commit protocol still costs hundreds of microseconds. A populated
column/row scene is required before making a claim about whole-solver snapshot
scaling. Sources: [`GuiGeometry.begin`, `commit`](../meltygui/core/rendering/gui_layout_prototype.py)
and [`EdgeGraph.fork`](../native/gui_prototype/src/edges.rs).

## Graphics capture and replay

The all-visible graphics phase run averages **22,664.5 us/frame**, compared with
17,664.5 us ordinary median. Exclusive categories, with their measured
instrumentation included:

| Phase | Mean us/frame | us/body | Share |
| --- | ---: | ---: | ---: |
| Retained composition | 5,333.3 | 15.64 | 23.5% |
| `_execute` remaining bookkeeping | 4,384.1 | 12.86 | 19.3% |
| Declaration wrapper remaining bookkeeping | 4,041.5 | 11.85 | 17.8% |
| Native dispatch/body outside separately timed descendants | 3,075.3 | 9.02 | 13.6% |
| ImGui context selection, input setup, new frame/window | 2,056.2 | 6.03 | 9.1% |
| Capture draw data and build/copy command packets | 1,360.8 | 3.99 | 6.0% |
| Layout begin/commit/captured | 973.7 | 2.86 | 4.3% |
| ImGui input finalization, end/window render | 455.4 | 1.34 | 2.0% |
| Capture-residency metadata | 346.3 | 1.02 | 1.5% |
| Eviction scan | 303.1 | 0.89 | 1.3% |
| Host renderer submission | 188.5 | 0.55 | 0.8% |
| Remaining phases and unassigned boundary time | 146.4 | 0.43 | 0.6% |

All 341 ImGui contexts already exist during measurement. This is **not** repeated
context creation. There are 341 new-frame/render/capture lifecycles nonetheless.
`cProfile` sees 341 `imgui.new_frame` calls and attributes about 1.07 ms to them
in its own slower run. Context-switching here means ImGui context selection;
it is not creation or switching of 341 OpenGL contexts.

Packet capture copies vertex/index data into Python bytes via **682
`ctypes.string_at` calls**, then passes bytes and command tuples through PyO3 to
`TextureCache.add_packet`. That method stores CPU packets; it does **not** upload
GPU buffers. Its Rust body takes only 16.2 us total in the native graphics run.
The surrounding Python capture, byte/tuple conversion and bindings cost much
more. The exact split between PyO3 conversion and Python allocator work is not
resolved independently. The scene has only 80 vertex bytes plus 24 index bytes
per packet, about **35.5 KB of captured vertex/index bytes per frame**.

### Inside native composition

The native graphics probe run averages **18,668.8 us/frame**. `render_many`
occupies 4,936.3 us (26.4% of that frame, 14.48 us/body). Its 341 `render_inner`
calls account for 4,905.1 us. Nonoverlapping stages inside those calls:

| Native replay stage | Mean us/frame | us/body |
| --- | ---: | ---: |
| Select scratch target, bind/clear FBO, set GL state | 1,156.5 | 3.39 |
| Upload own packet and draw it | 967.8 | 2.84 |
| Compose child textures | 663.5 | 1.95 |
| Resolve scratch accumulation into the retained texture | 2,050.5 | 6.01 |
| Inner-function remainder/probe boundaries | 67.0 | 0.20 |

The resolve is the implementation's straight-alpha conversion following
premultiplied accumulation. It performs another framebuffer bind and quad draw
for each dirty view. Source: [`TextureCache.render_many`, `render_inner`](../native/gui_prototype/src/texture.rs).

Individual GL entry-point probes give a second, **overlapping** decomposition
of the same replay workload:

| GL entry point | Calls/frame | CPU wall us/frame | Share of `render_many` |
| --- | ---: | ---: | ---: |
| `glBindFramebuffer` | 684 | 2,061.4 | 41.8% |
| `glBufferData` | 2,045 | 1,392.7 | 28.2% |
| `glDrawElementsBaseVertex` | 1,022 | 558.3 | 11.3% |
| `glClear` | 341 | 252.8 | 5.1% |
| Other GL/state/Rust work and probe boundaries | — | 671.2 | 13.6% |

Do not add this table to the stage table. The calls include driver validation,
submission and any blocking inside the call; they are **not GPU execution
durations**. The experiment does not distinguish which part of framebuffer-bind
latency is driver bookkeeping versus synchronization.

The call counts follow from the source: each of 341 views uploads/draws its own
packet and uploads/draws a resolve quad; 340 parent-child links add textured-quad
draws. Every scrollable view also uploads a child index buffer, even with an
empty child list. This yields 2,045 small buffer uploads and 1,022 draws, for
roughly 106 KB of total supplied buffer data. Small-call multiplicity dominates
the interpretation; these numbers are not bulk-transfer bandwidth measurements.

The enclosing GPU timer interval is 5.192 ms median in this run. It overlaps
CPU submission and is not added to the CPU frame time. Only the benchmark waits
for completion, outside the measured submission interval.

## Culling and depth checks

The already-mounted culled scene retains all 341 declarations but executes only
5 bodies and attempts 17 declarations each dirty frame. Ordinary CPU redraw is
174.2 us median; ordinary GL redraw is 486.7 us. The profile's CPU mean is 202.8
us, of which declaration costs 110.7 us (54.6%), execution bookkeeping 30.7 us
(15.2%), native/body dispatch 30.2 us (14.9%), and full-graph invalidation 7.5 us
(3.7%). Avoiding most bodies does not avoid the visible parents' child declarations.

In the culled GL phase run, the 611.6-us mean consists principally of declaration
162.4 us (26.6%), composition 100.0 us (16.3%), host submission 90.4 us (14.8%),
execution bookkeeping 74.1 us (12.1%), and native/body dispatch 60.9 us (10.0%).
The hot culled production scene is 99.8 us median. After cold eviction it has
128 scroll tiles at 32 MiB, so `_evict_scroll_tiles` returns early; it does not
pay the all-visible 680-query scan. Its 129 ImGui contexts include retained
offscreen captures, not 129 body executions.

A depth-32 chain costs 96.1 us uncached and 558.1 us retained redraw on the
production extension: approximately 3.00 and 17.44 us/executed node. In its
native profile, 31 visibility tests consume 28.5 us (0.92 us/test), versus
0.39 us/test at depth 5. Deep ancestry makes visibility queries more expensive,
but does not explain the bulk of retained dispatch in these cases. Native
identity/state lookup remains about 0.32 us/node for uncached calls. These
measurements do not establish arbitrary-depth asymptotic bounds.

## Next experiments, ordered by measured impact

1. **Fix the eviction work trigger experimentally:** keep eviction semantics,
   but avoid rescanning unchanged all-visible sets. The measured removable cost
   is about 237 us/hot frame here, and it explains the earlier hot-cache anomaly.
2. **Reduce repeated retained bridge work:** measure one change at a time in
   declaration/configuration/argument comparison and the per-view execution
   lifecycle. These are the dominant CPU-only costs. Do not bypass persistence,
   hotswap, change detection or cleanup to produce a misleading fast path.
3. **Investigate replay granularity and driver calls:** framebuffer binds,
   small uploads and resolve passes account for most native composition time.
   Measure batching or fewer capture boundaries with equivalent pixels/input
   behavior; do not assume the measured driver cost is all unavoidable GPU work.
4. **Measure populated layout transactions separately:** the current scene's
   native empty forks are negligible. A real columns/rows graph is needed to
   test whether snapshot sizes become a different bottleneck.
5. **Tune lower-level native resolution only after the larger costs:** argument
   and state-field resolution have measurable costs; identity lookup alone is
   not the dominant explanation. An allocator or ABI-specific experiment would
   be needed to resolve the explicitly unattributed boundary remainder.

No optimization from this list is applied by this profiling work.

## Reproduce and artifacts

`gui-overhead-profiles-2026-11-07.zip`, delivered through Hub Files, contains the
JSON samples/counters, raw `.prof` files, this report, harness/build scripts,
and the instrumented native source snapshot (no build target directory).
Each report records its arguments. Example commands:

```sh
.venv/bin/python tests/profile_gui_overhead.py --mode plain \
  --output /tmp/gui-profile/cpu-plain.json
.venv/bin/python tests/profile_gui_overhead.py --mode cprofile \
  --output /tmp/gui-profile/cpu-cprofile.json
.venv/bin/python tests/profile_gui_overhead.py --mode phases \
  --output /tmp/gui-profile/cpu-phases.json

# Destination must not already exist. The production extension is untouched.
.venv/bin/python tests/build_gui_profile_native.py \
  --destination /tmp/gui-profile/native-detailed
.venv/bin/python tests/profile_gui_overhead.py --mode native \
  --native-path /tmp/gui-profile/native-detailed/target/release/lib_gui_native.so \
  --variants gui_uncached,gui_redraw --output /tmp/gui-profile/cpu-native.json

# Run graphics commands through launch on a reserved agent seat and isolated XDG paths.
.venv/bin/python tests/profile_gui_overhead.py --graphics --variants gui_hot \
  --frames 150 --output /tmp/gui-profile/gl-hot-normal.json
.venv/bin/python tests/profile_gui_overhead.py --graphics --variants gui_hot \
  --frames 150 --eviction-limit 1000000 --output /tmp/gui-profile/gl-hot-limit.json
.venv/bin/python tests/profile_gui_overhead.py --graphics --culled --predeclare \
  --mode phases --variants gui_redraw,gui_hot --output /tmp/gui-profile/gl-culled-phases.json
```

The native probes and Python scopes use inclusive intervals internally. Only
exclusive Python phase values and explicitly nonoverlapping native sections are
summed in this report. The API-entry tables are alternative views of enclosing
work, and GPU intervals remain separate. Remaining attribution gaps are stated
rather than assigned to an assumed subsystem.

## Automatic selection follow-up

[Automatic raster selection](GUI_AUTO_CACHE.md) now includes matched
legacy/default/calibrated measurements, retained shared replay, actual workload
observations, native Wayland verification and residual CPU profiling. The earlier
tables above remain historical measurements, not the baseline for the new ratios.
