# Prototype benchmark rerun (2026-10-09)

**For the complete public decorator and window pipeline, see
[FULL_APP_BENCHMARKS.md](FULL_APP_BENCHMARKS.md).** The full application rerun
does not reproduce the speedup below: at 25 rows, its cached-boundary redraw
is slower than core render, and larger prototype runs expose a capture failure.
The measurements on this page remain component benchmarks only.
For the corrected implementation and its current full-application results,
see [PERFORMANCE_INVESTIGATION.md](PERFORMANCE_INVESTIGATION.md).

The native wrapper remains roughly 31–33 times faster than `core_render` on
identical collection bodies with caching disabled. This measures the original
immediate wrapper, **not the complete current retained `@gui` pipeline**.
The collision solver is faster on small layouts but approaches Python's cost
on a 512-cell pressure chain.

Machine: AMD Ryzen 9 7950X, Python 3.12.3, current release-built native extension.
Retained GPU measurements used NVIDIA GeForce RTX 4090. Benchmarks ran
sequentially in the normal desktop environment, without exclusive CPU isolation.

## Matched collection bodies versus core_render

Headless ImGui, real styling, cache disabled on both paths. Each frame executes
the same collection, button, integer and float Python bodies. Assertions verify
exactly `4 * rows + 2` calls, unchanged data and preserved input identity.
All rows execute; this is not a virtualized collection.

Median milliseconds, 100 measured frames after 25 warmup frames per row count:

| Rows | Calls | core_render | Native wrapper | Speedup | Time saved |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 25 | 102 | 22.853 | 0.742 | 30.8x | 96.8% |
| 100 | 402 | 92.145 | 2.790 | 33.0x | 97.0% |
| 300 | 1,202 | 277.614 | 8.368 | 33.2x | 97.0% |
| 1,000 | 4,002 | 931.809 | 27.831 | 33.5x | 97.0% |

A separate confirmation run (40 measured / 12 warmup) returned speedups of
30.8x, 32.8x, 32.8x and 32.6x respectively. Its raw samples and p95s are in
`benchmark_results_2026-10-09.json`. Native exclusive wrapper overhead was
approximately 3.3–3.6 microseconds per invocation across both runs.

The timer includes Python view bodies and ImGui command generation. It excludes
host/frame setup, retained graph/cache/collision bookkeeping, GPU rasterization,
composition, presentation, and OS input. The native path is `_bind_native`,
not `RetainedGui.gui`. Core render also implements substantially more behavior;
these are potential savings in an uncached subtree, not a feature-parity or
whole-application FPS claim.

Historical 25/100/300-row native times were 0.73/2.88/8.65 ms. The current results
are similar; the larger speedup ratios partly reflect variation in Python time.

## Collision solver comparison

Existing Python `edge_constraints.solve_edge` versus native `EdgeGraph.drag`.
300 measured / 30 warmup operations, alternating order. Graph construction is
excluded. Both restore the gesture's starting positions before each solve;
native result conversion is included. Every resulting edge position is checked
for equality after every pair of operations.

Cells start 100 pixels wide, with minimum 60, maximum 140 and both exterior
edges held as walls. A middle divider alternates positive, negative, repeated
and zero displacement. Local motion is 30 pixels; pressure motion is large
enough to saturate the chain. These are bounded synthetic chains, not an entire
window manager or native-surface benchmark.

Median microseconds:

| Cells | Local Python | Local Rust | Speedup | Pressure Python | Pressure Rust | Speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 3.190 | 0.570 | 5.60x | 3.170 | 0.580 | 5.47x |
| 8 | 5.820 | 1.050 | 5.54x | 7.515 | 1.370 | 5.49x |
| 32 | 17.170 | 3.990 | 4.30x | 25.745 | 5.785 | 4.45x |
| 128 | 70.326 | 23.035 | 3.05x | 108.001 | 42.741 | 2.53x |
| 512 | 314.790 | 215.998 | 1.46x | 471.417 | 458.466 | 1.03x |

Scaling deserves attention before production: the Rust implementation clones
snapshots and repeatedly scans cell collections. The Python solver has adjacency
indexes. The shrinking advantage is consistent with that difference, although
these measurements do not separately attribute the cost to individual steps.

## Current retained cache and layout pipeline

Separate absolute measurements, **not a core_render comparison**. Uses the
two-column regression scene: a 400x200 parent and two solid-color child textures.
100 measured / 20 warmup frames. Timing includes invalidation or drag, root call,
flush and `glFinish` to wait for offscreen GPU work. Presentation and native
window events are excluded. Execution counts and raster passes are asserted.

| Operation | Median ms | p95 ms | Python bodies executed | Raster passes |
| --- | ---: | ---: | --- | ---: |
| Unchanged cached root | 0.015 | 0.020 | None | 0 |
| Invalidate one child | 0.313 | 0.502 | That child | 2 |
| Move divider | 0.576 | 0.780 | Both children, no parent | 3 |
| Invalidate all three views | 0.470 | 0.672 | All three | 3 |

Independent invalidation works, and a hot cache avoids both Python bodies and
rasterization. Divider motion costs more than a full recapture of this tiny
scene: geometry work outweighs the cheap parent body it avoids. More expensive
ancestors are needed to quantify the savings from avoiding parent execution.
This result is not a claim that independent invalidation is always faster.

## Reproduce

Run sequentially from the checkout; do not benchmark these concurrently:

```sh
.venv/bin/python tests/benchmark_gui_prototype.py --rows 25,100,300,1000 --frames 100 --warm 25 --json-out /tmp/gui-wrapper.json
.venv/bin/python tests/benchmark_gui_collisions.py --json-out /tmp/gui-collisions.json
.venv/bin/python tests/benchmark_gui_retained.py --json-out /tmp/gui-retained.json
```

The first two are CPU-only. The last creates an invisible GLFW GL context and
requires a working graphics session. No application implementation was changed
for these measurements.
