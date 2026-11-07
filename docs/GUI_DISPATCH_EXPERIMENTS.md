# Retained dispatch experiments after automatic raster selection

Measured on 2026-11-07 using the current workspace, CPython 3.12, Ryzen 9
7950X and NVIDIA RTX 4090 desktop OpenGL. These are isolated experiments;
the installed extension and production Python modules are unchanged.

That statement describes these isolated experiments. Later production packet
and bookkeeping changes are recorded in [implementation results](GUI_PACKET_BOOKKEEPING_RESULTS.md).
The historical measurements below have not been rewritten as current results.

## Result

**Applicability correction:** the 14.3% result below is the hostless reference
harness. Normal `@os_window` surfaces call `_GuiWindow.solve_native` before
drawing, which permanently attaches `NativeCollision` and native surface/screen
edges. Those hosted views do **not** qualify for the empty-layout fast path,
even when the app declares no rows or columns. Its 0.83 ms saving is therefore
not a general hosted-app improvement. The host-connected control below keeps
the real native layout bridge and original transaction snapshots active.

Randomized frame-interleaved full redraw falls from **14.104 ms to 12.087 ms**
with the combined experiment, including a fused retained dispatch bridge
(14.3% lower median). Raw drawing is 0.255 ms. This is a bounded improvement,
not a resolution of the retained CPU gap. Unchanged replay is 0.148 ms baseline and 0.150 ms fused.

The primary scene has 341 trivial bodies, checksum 58,311, depth five and
fanout four. All candidates retain 341 contexts and distinct IDs, execute all
341 bodies on redraw and zero on hot replay, and match raw pixels exactly.
Automatic cases keep one raster, 262,144 raster bytes, 35,464 packet bytes
and one 262,144-byte scratch target. No context sharing, state removal,
uncached bypass, per-frame readback or production GPU wait was introduced.

## Attribution and hypotheses

The current ordinary-extension cProfile control ranks `_execute` (4.514 ms
self), declaration (4.243 ms), and the public/native wrapper boundary
(2.515 ms) first. ImGui `new_frame` contributes 1.211 ms. Its instrumented
frame median is 29.370 ms; these values are attribution, not performance
estimates for ordinary execution. They agree with the priorities in
[automatic cache residual profiling](GUI_AUTO_CACHE.md#residual-profiling).

Three bounded hypotheses were tested:

1. **Empty layout transactions.** A body with no layouts, frames, bindings,
   portals, native layout or gesture, and two pristine native graphs, does not need two empty graph forks and
   copies of empty maps. It records an explicit empty transaction. A failed
   body that introduces its first geometry restores empty graphs and maps;
   populated geometry continues through the original transaction path. Native
   graph statistics must be `(0, 0, 0, 0)`: empty Python maps alone are
   insufficient because retired graphs retain versions and edge serials.
2. **Narrow native bridges.** One graph call returns the invocation and the
   geometry actually consumed by execution instead of constructing nine-field
   `info` dictionaries. Packet capture copies live ImGui CPU buffers directly
   into owned Rust vectors, avoiding intermediate Python `bytes` allocations
   and the two `ctypes.string_at` calls. Texture-ID owners and nested parent
   keepalive restoration are unchanged.
3. **Fused retained dispatch.** A dedicated native call performs native
   begin/call/end with the same argument handling and cleanup. The Python
   wrapper still checks function code changes and recompiles/reconfigures the
   parameter plan in place. This removes some Python/native calls and kwargs
   forwarding without dropping injection or the full draw state.

## Matched measurements

### Host-connected control: relevant to normal hosted apps

`--native-layout` calls the real `_GuiWindow.solve_native(adapter)` before
mounting and inside every timed frame. The adapter supplies a deterministic
128 by 512 fixed surface, while the actual `NativeCollision`, graph walls,
native commit and original layout snapshots remain active. This is a real
host-layout bridge control with a fixed platform seam, not a full native
Wayland application measurement. It uses 24 warm and 200 measured frames.

| Host-connected variant | CPU median ms | p95 ms | MAD ms |
| --- | ---: | ---: | ---: |
| Raw | 0.268 | 0.348 | 0.014 |
| Legacy per-view raster | 20.191 | 23.668 | 0.694 |
| Auto baseline | 15.768 | 18.635 | 0.590 |
| Empty layout transaction only | 15.844 | 18.837 | 0.547 |
| Metadata and packet bridge only | 14.756 | 17.345 | 0.693 |
| Both above | 14.766 | 17.789 | 0.580 |
| Both plus fused dispatch | 14.450 | 17.337 | 0.628 |

Empty-layout specialization produces no hosted saving because its guard
correctly falls back. The native bridge saves 6.4%; adding fused dispatch
brings the matched reduction to 8.4%. Hot baseline/fused medians are
0.184/0.189 ms. Automatic GPU medians are all 25.600 us. Pixels, 341 body
executions/checksum 58,311, contexts/IDs, 35,464 packet bytes, one raster and
one scratch target match. Both native axes retain four edges, one constraint,
version seven and zero solver visits. Source inspection confirms the host
attachment persists for the surface lifetime, so the hostless fast path is
not regained after ordinary startup. Single cold samples are 39.700 ms
baseline, 37.892 ms bridge and 37.221 ms fused; these are not distributions.

### Hostless reference harness: bounded specialization

The same-process comparison randomizes variant order every frame, uses 24
warm and 200 measured frames, freezes conservative costs, and disables
calibration workers/probes before timing. CPU submission and GPU elapsed
intervals remain separate. Completion waits and final pixel reads live only
in the harness. All source changes in the native comparison build are
uninstrumented; its baseline path uses the unchanged original methods.

| Variant | CPU median ms | p95 ms | MAD ms | GPU median us |
| --- | ---: | ---: | ---: | ---: |
| Raw | 0.255 | 0.287 | 0.007 | 4.096 |
| Legacy per-view raster | 18.519 | 21.329 | 0.472 | 5490.176 |
| Auto baseline | 14.104 | 16.022 | 0.340 | 29.696 |
| Empty layout transaction only | 13.270 | 15.122 | 0.373 | 29.696 |
| Metadata and packet bridge only | 13.118 | 14.832 | 0.365 | 29.184 |
| Both above | 12.375 | 14.232 | 0.334 | 28.672 |
| Both plus fused dispatch | 12.087 | 14.081 | 0.333 | 29.696 |
| Hot baseline | 0.148 | 0.180 | 0.006 | 5.120 |
| Hot fused | 0.150 | 0.194 | 0.007 | 5.120 |

Multiple live comparison scenes and randomized ordering change absolute
timings relative to the smaller original policy matrix. GPU intervals include
submission gaps and driver scheduling. Neither clock is added to the other.

Cold mount samples (one per variant, including harness completion): raw
0.452 ms, legacy 77.903 ms, auto baseline 39.781 ms, empty-layout 35.585 ms,
native bridge 37.923 ms, combined 38.161 ms and fused 37.647 ms. These are
single observations, not cold-start medians. An additional hot-baseline mount
took 60.847 ms, demonstrating cold variability.

The ordinary installed-extension policy matrix was reproduced first with
16 warm / 60 measured frames: raw 0.243-0.252 ms, legacy 17.567 ms,
defaults 12.954 ms, frozen calibrated 14.550 ms. Default and calibrated
retain the same representation. This variation is why candidate conclusions
use frame-interleaved controls, not a favorable sequential baseline.

Before strengthening the native-graph guard, an eight-run forward/reverse component sequence (200 frames each) retained
the variation: baseline 13.631/14.378 ms, empty-layout 12.101/12.065 ms,
native bridge 13.818/12.952 ms, combined 10.933/12.108 ms. Raw varied
0.238-0.262 ms. Every run passed counts/checksums and pixels.

### Controls beyond trivial visible bodies

These final guarded comparisons use 24 warm and 100 measured frames.

| Workload | Raw ms | Auto baseline median/p95 ms | Fused median/p95 ms |
| --- | ---: | ---: | ---: |
| 341 bodies, 512 integer operations each | 9.396 | 23.543 / 24.840 | 21.477 / 22.516 |
| Fully predeclared culled tree, five executing bodies | 0.240 | 0.558 / 0.709 | 0.517 / 0.645 |

Heavy bodies execute 341 times with checksum 733,500,616,135; the roughly
2.07 ms absolute improvement persists. The culled raw control intentionally
traverses all 341 nodes, while retained variants execute five (checksum 15).
Both retained culled cases preserve 341 IDs and 257 resident contexts after
normal eviction, 26,728 packet bytes, one raster and one scratch target.
They match raw pixels exactly. Culled GPU medians are both 13.312 us.
Heavy-body GPU medians are 27.136/28.672 us baseline/fused; the absolute
difference is small and is not presented as a GPU improvement.

The final fused cProfile run still attributes 4.074 ms self to declaration,
3.806 ms to `_execute`, 2.249 ms to the native retained call boundary and
1.105 ms to ImGui `new_frame`. Instrumented median is 25.137 ms; these are
not ordinary performance timings. Python declaration/execution orchestration
continues to dominate after the bounded changes.

## Correctness and limits

The first combined candidate passed 155 focused CPU and real GL tests.
The final guarded fused candidate passed 189 tests covering widgets/focus, cache-mode
transitions, positions/scrolling, partial redraw, layout/rollback, persistence,
identities, parameter/default injection, mutable and scalar returns, source
code replacement, texture ownership and cleanup. Twelve additional regression
cases specifically introduce first rows, columns, a portal or a successful
nested child layout before the parent raises, and then retry successfully.
They cover pristine, residual-edge and retired-edge native starting states.
The first unguarded candidate was rejected during review: resetting an empty
but previously used native graph would lose versions and serials. Only the
guarded results above describe the final candidate.

An additional hosted regression test asserts that a body introducing its
first layout still receives original transaction snapshots and restores
native walls/versions after failure. It and the twelve existing rollback
controls pass together (13 tests). This closes the applicability check;
it does not make the empty-layout specialization eligible for hosted views.

The raw-pointer packet bridge is experimental and must remain internal to
the render-thread binding with buffer lifetime guaranteed. It synchronously
copies CPU draw buffers; it does not retain their pointers or read GPU memory.
The process-local source replacements are a measurement mechanism, not a
production hotswap implementation. Native-module live reload, a noneditable
wheel, CUDA, macOS, Windows and native Wayland presentation are not validated
by this experiment. GLFW/OpenGL is the exercised backend.

## Reproduction and recommendation

Use a reserved agent desktop and isolated XDG paths. Artifacts are under
`/tmp/astra-gui-overhead/`; JSON includes individual CPU/GPU samples, cold
observations, resource counters and expected body checksums.

```sh
.venv/bin/python tests/build_gui_dispatch_experiment.py \
  --fused-dispatch --destination /tmp/dispatch-native
.venv/bin/python tests/compare_gui_dispatch_experiments.py \
  --native-path /tmp/dispatch-native/target/release/lib_gui_native.so \
  --output /tmp/dispatch-interleaved.json --frames 200 --warm 24
# Host-connected control, retaining native layouts and original snapshots:
.venv/bin/python tests/compare_gui_dispatch_experiments.py \
  --native-path /tmp/dispatch-native/target/release/lib_gui_native.so \
  --native-layout --output /tmp/dispatch-hosted.json --frames 200 --warm 24
```

`tests/run_gui_dispatch_experiments.py` reproduces separate-process
forward/reverse controls. `tests/gui_dispatch_experiment.py` can run the
existing profile/matrix harness or pytest with a selected process-local
candidate. The native builder copies current workspace source, never a stale
revision, and never installs the result.

The earlier recommendation to ship empty-layout specialization first is
withdrawn for normal hosted applications: its fast path cannot run there.
It is only a hostless-runtime specialization. Prioritize the narrow native
metadata/packet bridge after settling its binding ownership contract, and
measure its two parts independently before selecting the smallest production
patch. Fused dispatch is a separate incremental option. The host-connected
control, not the larger hostless combined saving, should guide that decision.

Even the fused candidate remains roughly 47 times raw. Larger gains require
reducing declaration/execution orchestration and native round trips while
retaining their full semantics. Per-view ImGui lifecycle work remains, but
the measured `new_frame` contribution alone cannot explain the residual gap.
Sharing contexts or omitting input/state cannot be inferred as valid from
these timings and was not used.
