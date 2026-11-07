# Bounded GUI optimization experiments

Measured on 2026-11-07, using the existing overhead harness, CPython 3.12,
Ryzen 9 7950X and NVIDIA RTX 4090 OpenGL. All graphics runs used a reserved
agent desktop and isolated XDG paths. Existing applications were left alone.
These measurements concern the opt-in Rust `@gui` prototype.

## Accepted: change-driven eviction eligibility

An unchanged all-visible scene exceeded the 256-tile soft limit with 340
protected visible tiles. Both flushes rescanned that unchanged set each frame.
The fix retains the same limits and protection rules. Native geometry/topology
revisions, capture residency changes, input protections and budget changes
invalidate the last eviction decision. An unchanged decision returns immediately.
Measured-size updates, including the native direct-refresh path, also invalidate
the decision. The revision query first consumes pending native geometry edits.

| Uninstrumented all-visible hot scene | CPU median | CPU p95 |
| --- | ---: | ---: |
| Original code, 300 measured / 30 warm frames | 328.06 us | 410.40 us |
| Accepted trigger, 1000 measured / 50 warm frames | 95.68 us | 109.77 us |
| Same-code forced-rescan control, 1000 / 50 | 326.78 us | 343.95 us |

The accepted trigger reduces the matched control median by about 71%. All three
runs match the raw-renderer pixels, execute zero view bodies and retain exactly
341 declarations, 341 contexts, 340 tiles and 1,048,576 tile bytes. GPU medians
are 2.048-3.072 us at this timer's quantization scale. A first shorter candidate
run measured 117.15 us; the raw
samples retain that workstation variability rather than hiding it.

The fully predeclared culled scene remains at 99.85 us hot and 526.94 us on
redraw (five bodies). This scene stays within the tile budgets and uses the
existing immediate budget check.

Regression checks cover unchanged hot scans, scroll, explicit and measured
sizes, placement, content offsets, new residency, memory budgets, focus,
capture, active widgets, nested portal protection and preserved native state.
Real GL tests cover tile/context release, revisit, recursive capture, pixel
composition, alpha, clipping and bounded scratch allocation.

Reproduce with `tests/profile_gui_overhead.py --graphics --variants gui_hot
--frames 1000 --warm 50 --output RESULT.json`, launched through the reserved
desktop tools. Add `--eviction-rescan` for the benchmark-only control. This
forces eligibility reevaluation without changing tile budgets or production
behavior. For culling, add `--culled --predeclare`.

Raising the global tile limit was rejected: it would conceal the trigger cost
and change memory behavior. No instrumented extension replaces the normal
extension. Native live reload, Wayland presentation, macOS, CUDA and an external
noneditable wheel have not been validated by this experiment.

## Accepted: lazy input acknowledgement

The bridge previously read a node's retained result on every redeclaration,
including unchanged caller input and pending edits. It now reads that result
only when a changed, nonpending input needs to be checked for acknowledgement.
Argument comparison, pending-edit handling, identity, state configuration,
aliases and final result consumption remain in their original order.

For the 341-body CPU redraw, a 300-frame original/candidate pair measured
5.867/5.787 ms. A 1000-frame eager-control/lazy pair measured 5.761/5.634 ms
(p95 6.896/6.546 ms), about 2.2% lower median. Both execute exactly 341 bodies
with the expected checksum. The gain is small compared with workstation
variability (MAD 193/186 us); it does not explain away the multi-millisecond
retained bridge cost. The behavioral test verifies that unchanged declarations
skip the unconsumed result read and changed input still performs it.

Reproduce with `tests/profile_gui_overhead.py --variants gui_redraw --frames
1000 --warm 50 --output RESULT.json`. Add `--eager-input-acknowledgement` for
the process-local old-behavior control. Neither benchmark flag changes the
installed runtime.

## Experimental only: reuse quad indices during replay

`tests/build_gui_replay_experiment.py --destination DIRECTORY` builds an isolated
uninstrumented extension. It omits the final six-index upload when child
composition has already uploaded those exact indices to the same element
buffer. No packet, texture, capture boundary or memory budget changes. The
all-visible scrollable tree removes 341 redundant element-buffer uploads per
redraw. The normal extension and production texture source remain unchanged.

| Sequential GL runs, 300 measured / 30 warm | Redraw CPU median | Hot CPU median |
| --- | ---: | ---: |
| Normal extension, first | 18.188 ms | 95.63 us |
| Isolated variant, first | 17.343 ms | 108.75 us |
| Isolated variant, repeat | 17.564 ms | 98.91 us |
| Normal extension, repeat | 17.878 ms | 102.93 us |

Every run matches raw pixels, expected body counts/checksums and retained
resources. The apparent redraw reduction is 1.8-4.6%; substantial run variation
prevents treating this as a stable promised gain. The variant passes 71 GL
tests covering partial edits, scroll/revisit, recursive capture, alpha/clipping,
fractional geometry, widgets, and hard-budget/scratch-pressure behavior.
It remains isolated for further driver/backend measurements. It is not a
shared-parent-capture redesign and makes no claim about that architecture.

Pass the built `DIRECTORY/target/release/lib_gui_native.so` to the existing
profile harness with `--native-path` to reproduce the variant runs. The raw
results retain p95, MAD and every sample.

## Measured only: populated layout transactions

`tests/benchmark_gui_layout_transactions.py` mounts independent two-cell layouts,
alternating rows and columns, and measures a native fork pair, a complete
begin/abort transaction, and one invalidated leaf redraw. It asserts unchanged
edge statistics, layout/binding counts, and exactly one body execution per
redraw. There is no GL work in this experiment.

| Resident layouts | Bindings | Native fork pair | Begin + abort | One leaf redraw |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 0.42 us | 1.67 us | 17.69 us |
| 16 | 32 | 1.86 us | 9.57 us | 137.32 us |
| 64 | 128 | 6.76 us | 40.27 us | 543.53 us |
| 256 | 512 | 28.46 us | 185.54 us | 2,316.65 us |

Each median uses 300 samples after 20 warmups. Begin/abort includes Python
layout snapshots and rollback, so it is not a pure begin-time estimate. One
leaf redraw also includes global layout application/commit work; it is not
explained by native copying alone. No solver optimization was shipped. The
next layout investigation should isolate whole-map application/commit before
redesigning the native fork operation.

## Validation and limits

The accepted runtime passes all 241 selected `test_gui_*.py` and
`test_retained_gui_*.py` tests on the reserved desktop, using the ordinary
uninstrumented extension. The isolated replay extension separately passes 71
focused GL tests. These are CPU plus GLFW/OpenGL checks on one NVIDIA driver;
they do not establish other GPU/backend behavior or native live reload.

No renderer-wide batching, shared-parent captures, layout ownership changes,
or per-frame screenshot/readback work was introduced. The Python declaration,
transaction and capture bridge remains the dominant redraw coupling.

## Automatic selection implementation

[Automatic raster selection](GUI_AUTO_CACHE.md) now includes matched
legacy/default/calibrated measurements, retained shared replay, actual workload
observations, native Wayland verification and residual CPU profiling. The earlier
tables above remain historical measurements, not the baseline for the new ratios.
