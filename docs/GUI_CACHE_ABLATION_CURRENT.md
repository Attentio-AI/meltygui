# Feature removal from the production cache without injection

For the declaration wrapper's individual source lines and narrower verified
cuts, see [declaration line profiling](GUI_DECLARATION_LINE_PROFILE.md).

November 7, 2026, shared-context DrawCapture implementation. CPython 3.12,
meltygui-imgui 2.0.4, Linux GLFW/OpenGL, NVIDIA RTX 4090.

The largest removable costs in this fixed scene are dynamic declarations,
layout, and renderer setup/grouping/measurement. Removing all eight feature
families cumulatively reduces CPU submission from **11.849 to 2.485 ms**,
approaching the **2.124 ms** minimal-cache reference in the same runs.

These are destructive, isolated benchmark variants, not production fixes.
They deliberately stop supporting changes to the mounted tree, geometry,
inputs or resources. Library runtime code and the installed extension were
not modified. The cache-only baseline already has no dependency injection.

## Method

Each mode ran twice with randomized frame-interleaved ordering, seeds 1027
and 2027, 24 warmup and 200 measured frames per variant. There are 12,400
measured frames across the six runs. Tables average equal-sized repeats.
Each removal is installed after the normal mount. Timings are uninstrumented
CPU submission times; no cProfile overhead is present. GPU completion waits,
pixel checks and resource assertions are outside the CPU timer.

All variants redraw the same 341-view tree, one rectangle per body, without
artificial computation. Each frame asserts 341 body executions, checksum
58,311, 341 packet uploads and one raster refresh. Initial/final pixels match
exactly, and capture identities and GPU resource sizes remain stable. All
variants passed these checks in smoke runs and both measured repeats.

The isolated native extension was rebuilt from current source. The renderer
cut now finds the renderer call structurally rather than matching stale
indentation. The obsolete per-view input-apply cut was removed: that work no
longer exists in the shared-context implementation. Fine cuts for validation,
capture residency and retirement were added.

## Independent removals

Every row removes only the named family from the intact cache-only baseline.
Baseline mean is **12.047 ms**. Savings are paired differences within each run.
These savings overlap and must not be summed.

| Removed family | CPU remaining ms | Mean saved ms | Saved in each repeat ms |
| --- | ---: | ---: | ---: |
| Dynamic declaration/state/argument reconciliation | 8.997 | **3.050** | 3.136 / 2.964 |
| Layout transactions, binding and native geometry updates | 10.057 | **1.990** | 2.055 / 1.924 |
| Renderer setup, state synchronization, ImGui groups and measurement | 10.226 | **1.821** | 1.837 / 1.804 |
| Host input routing and per-view input finalization | 11.358 | **0.689** | 0.642 / 0.735 |
| Automatic raster selection and live calibration | 11.587 | **0.459** | 0.472 / 0.447 |
| Residency, eviction, retirement and source/dirty scheduling scans | 11.645 | **0.401** | 0.345 / 0.457 |

The declaration cut uses mounted values/arguments/geometry and fixed keys,
while retaining graph declare/begin/commit. It removes more than state.configure:
caller/default construction, identity/binding lookup, invocation reconciliation,
change detection, alias handling and argument validation also disappear.

The renderer cut uses the fixed benchmark body signature and preserves nested
identity enter/leave and exception cleanup. It bypasses CacheRenderer setup,
ImGui ID/group scopes, measurement, parameter forwarding/state synchronization
and accounting. It still draws the real body and retains the surrounding native
runtime begin/end. Its saving is not a measurement of ImGui grouping alone.

## Finer independent removals

These are separate runs with an **11.812 ms** baseline. Each row again removes
one operation family from the intact baseline, not from another row.

| Removed operation | Mean saved ms | Saved in each repeat ms |
| --- | ---: | ---: |
| Layout transaction begin/abort, including snapshots and setup | **0.682** | 0.708 / 0.656 |
| Native geometry commit | **0.603** | 0.578 / 0.628 |
| Declaration-time state.configure | **0.400** | 0.338 / 0.463 |
| Host input processing with a frozen idle snapshot | **0.320** | 0.301 / 0.340 |
| Capture residency accounting and ancestor traversal | **0.311** | 0.331 / 0.291 |
| Per-view input finalization | **0.223** | 0.242 / 0.205 |
| Repeated height clamp/validation and tint normalization/validation | **0.190** | 0.167 / 0.212 |
| Retirement polling | **-0.020** | -0.023 / -0.017 |

Retirement polling has no demonstrated benefit from removal in this workload;
the tiny slowdown should not be interpreted as an optimization opportunity.
The validation cut reuses mounted height/background values. The transaction
cut removes all of geometry.begin/abort, not just the dictionary copies.
No error recovery, moving geometry or changing tint is exercised by these cuts.

## Cumulative removal

Each row removes another family on top of all preceding cuts. This mode has
its own **11.849 ms** baseline. Incremental savings depend on removal order;
only this table telescopes to the final measured total.

| Additional removal | CPU remaining ms | Incremental saving ms |
| --- | ---: | ---: |
| Baseline | 11.849 | — |
| Input | 11.264 | 0.585 |
| Layout | 9.331 | 1.933 |
| Policy/calibration | 9.061 | 0.270 |
| Renderer setup/grouping/measurement | 7.561 | 1.500 |
| Dynamic declarations | 4.976 | 2.585 |
| Lifetime accounting and scheduling scans | 4.686 | 0.290 |
| Graph execution/results/measurement and composition scheduling | 3.608 | 1.078 |
| Remaining execution glue, replaced with minimal capture procedure | **2.485** | **1.123** |
| Separate minimal-cache reference | 2.124 | Not another cut |
| Raw drawing reference | 0.287 | Not another cut |

The graph cut requires the earlier fixed-topology, layout, lifetime, policy and
renderer cuts. It reuses verified child placements but recaptures and uploads
all bodies on every frame. The last step borrows the same GPU cache, capture
buffers and texture owners through the minimal capture bridge; the production
runtime remains their sole closer. The remaining 0.361 ms above the separate
minimal cache includes retained public host and fixed declaration dispatch;
that residual has not been independently attributed.

The result supports targeting repeated declaration and geometry work first.
It does not establish that those features can be omitted safely in real apps.
The old multi-context input-copy result is historical: current full input
removal saves 0.689 ms, not the previously measured roughly 3.5 ms.

## Reproduction and coverage

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/cache-cuts-native
.venv/bin/python tests/benchmark_gui_cache_ablation.py \
  --native-path /tmp/cache-cuts-native/target/release/lib_gui_native.so \
  --mode single --frames 200 --warm 24 --seed 1027 --output /tmp/cache-cuts/single-1027.json
```

Repeat for modes `detail` and `cumulative`, and seed 2027, using distinct output
names. Run graphics commands on a reserved agent desktop with isolated XDG
config/state/cache directories. Raw samples, scripts and logs for this run are
under `/tmp/codex-cache-ablation-current/`; the isolated native build is at
`/tmp/codex-cache-ablation-current-native/`.

The rebuilt extension passed 28 focused cache-only, minimal-cache, public-GUI
and cache-policy tests. Scope is fixed-topology desktop OpenGL rendering with
native geometry and idle input. Active interaction, CUDA, full Wayland
presentation, macOS/iOS and release wheels were not tested. Most production
bookkeeping remains inherited in the baseline; each cut deliberately disables
its named behavior only within its experimental cache instance.
