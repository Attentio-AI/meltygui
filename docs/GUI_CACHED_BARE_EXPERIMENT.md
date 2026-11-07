# Minimal caching and the complete automatic cache path

November 7, 2026. CPython 3.12, Linux GLFW/OpenGL, RTX 4090. All changes are
isolated benchmark code. The installed extension and public runtime are unchanged.

## Minimal per-view cache

`CachedBareGui` builds on the forwarding-only Rust wrapper. It owns an explicit
integer-keyed result table, invalidation generation, recursion depth and a capture
bridge. Cache hits skip the Python body. Misses call the same body with explicit
arguments, with no parameter plan or draw-state injection. Python ImGui capture
glue uses the existing Rust `TextureCache`, per-view contexts, typed byte packet
capture, logical child references and shared replay into a root raster.

This is a fixed-tree experiment: callers must invalidate after input, geometry,
source or texture changes. It has no dependency tracking, automatic layout,
input routing, retirement/eviction or independent leaf scheduling. It uses a
fixed root-raster policy, not the production automatic selector. GL resources
are explicitly closed on their owning thread. The Rust table uses short locks
which end before Python callbacks and result destructors; it is GC traversable.

Two randomized frame-interleaved runs, each with 30 warm and 300 measured frames,
draw the same 341-node scene with one rectangle per body. The following are
arithmetic mean CPU submission times averaged across the two runs:

| Path | Mean ms/frame |
| --- | ---: |
| Raw | 0.257 |
| Bare Rust forwarding | 0.382 |
| Minimal cached, all bodies recaptured | 3.079 |
| Minimal cached, packets recomposed with no bodies | 0.294 |
| Minimal cached, unchanged root raster | 0.091 |
| Full GUI, all bodies recaptured | 16.770 |
| Full GUI, unchanged replay | 0.396 |

Basic per-view capture and caching adds about 2.70 ms over bare forwarding in
this scene. It does not explain the whole full-GUI gap. The computation-heavy
control (512 integer operations per view) gives 12.26/12.75 ms for minimal
cached redraw, versus 26.47/27.30 ms for full GUI. Hot minimal replay executes
zero bodies, so its cost does not include the skipped computation.

Both minimal and full automatic caches mount with exactly 341 contexts and
logical references, 35,464 packet bytes, one 262,144-byte raster and one
262,144-byte scratch target. Thus the minimal result is not from replacing the
whole tree with a single capture context. Cold mounts are single samples,
not distributions: minimal cached mounts ranged 7.2-12.8 ms and full GUI
34.3-37.8 ms in these two runs, including GPU completion.

## Full production caching and automatic selection

The full-cache experiment preserves the production retained declaration,
NativeState, identities, graph, layout, input, capture, calibration, policy,
invalidation, independent execution, composition, retirement and presentation.
It compares normal execution with a fixed Rust call to the benchmark body's
explicit six-argument signature. The latter bypasses `Renderer.__call__`,
including its parameter matching, injected values and per-body draw-state
dispatch. Runtime begin/end and declaration-time state configuration remain.

This is deliberately a narrower ablation than the minimal cache. The complete
cache requires internal retained state. The fixed-body path does not expose
that state to the body, but does not remove it from the framework. Neither the
full-to-minimal gap nor the whole fixed-body frame time is a measurement of
dependency injection alone. Bypassing Renderer also removes its ImGui group
and result/state dispatch work. The fixed ABI is an experimental adapter, not
a general replacement decorator.

Two runs per scenario, 30 warm and 200 measured frames, randomized order, real
native layout with a fixed surface adapter, idle host input, and normal live
calibration enabled. Values are arithmetic mean CPU submission milliseconds:

| Workload | Full GUI, auto | Fixed body, auto | Full GUI, per-view rasters | Fixed body, per-view rasters |
| --- | ---: | ---: | ---: | ---: |
| Full redraw | 16.871 | 13.618 | 21.208 | 18.096 |
| One leaf recaptured | 2.644 | 2.614 | 0.709 | 0.686 |
| Root recomposed, no bodies | 2.466 | 2.481 | 0.425 | 0.421 |
| Unchanged replay, no bodies | 0.332 | 0.330 | 0.332 | 0.332 |

Automatic selection improves full redraw by about 4.34 ms relative to keeping
341 rasters. With fixed body dispatch the improvement is about 4.48 ms.
Bypassing Renderer saves about 3.25 ms during full redraw, but almost nothing
when no bodies execute. That is execution-time dispatch/plumbing cost, not all
declaration or dependency-management work.

Automatic selection does not improve every workload. In this small-packet tree
it retains one root raster and promotes no internal view. A leaf edit therefore
recomposes the shared tree; per-view raster mode updates five textures along
the leaf's ancestor chain. The automatic policy performs about 44,109 candidate
evaluations over each warm-plus-measured full/leaf/recompose run. This count is
not a measurement of selector CPU time, but illustrates work absent from the
minimal fixed policy. Hot runs perform no raster work and stop at 340 evaluations.

GPU elapsed intervals are separate. Full redraw with automatic caching has
about 29.7 microseconds GPU elapsed median, versus 5.4-6.0 milliseconds in the
341-raster modes. These intervals include submission gaps; do not add them to
CPU submission time or interpret their difference as shader work alone.

## Calibration and policy controls

The live runs use conservative operation priors plus passive replay samples;
busy full redraws do not satisfy the production cheap-frame condition for
active calibration probes. A frozen-prior control measured 17.269/13.871 ms
for full/fixed automatic redraw, consistent with the live comparisons.
Sixteen rectangles per body measured 18.663/15.476 ms for full/fixed automatic
redraw, versus 22.377/19.116 ms with per-view rasters.

Completed-calibration controls used measured operation costs plus all 341 view
replay observations gathered on a separate scene. Every variant received the
same frozen snapshot and observations; automatic selection remained active.
Two repeats produced the following mean CPU milliseconds:

| Workload | Full GUI, auto | Fixed body, auto | Full GUI, per-view rasters | Fixed body, per-view rasters |
| --- | ---: | ---: | ---: | ---: |
| Full redraw | 17.290 | 13.911 | 21.690 | 18.493 |
| One leaf recaptured | 2.723 | 2.717 | 0.714 | 0.702 |
| Root recomposed, no bodies | 2.438 | 2.425 | 0.399 | 0.399 |

Measured costs preserve the conclusion. One leaf-update repeat promoted one
internal raster and the other promoted none; this did not reverse the CPU
tradeoff. Full-redraw repeats retained only the root raster. An earlier control
sampled only the root workload; those superseded files are preserved under
`/tmp/codex-gui-full-cache/root-observation-only/` and are not used in these tables.

The separate existing `benchmark_gui_cache_selection.py` workload uses 1,024
overlapping rectangles in a stable heavy child while its sibling changes.
With measured costs and workload observations, automatic caching promotes the
heavy child and retains two rasters rather than three. CPU median was 0.219 ms
versus 0.205 ms for per-view rasters, while GPU elapsed median fell from 19.456
to 13.312 microseconds. If the heavy child changes every frame, the selector
keeps only the root raster. This verifies actual workload-dependent selection,
not merely that the auto-cache flag was enabled.

## Verification and reproduction

Exact pixels match between variants, including after explicit geometry edits
in the minimal cache and independent leaf-content edits in the full cache.
Body counts/checksums, state/context identities, cache bytes and raster work
are checked. The cached-wrapper/public API suite passed 17 tests, including
nested capture failure/context restoration and resource cleanup. The production
automatic cache and policy suite passed 20 tests. The final combined run against
the isolated extension passed all 37 tests. Coverage is desktop OpenGL;
CUDA, native Wayland presentation, other operating systems and release wheels
were not tested.

On a reserved desktop with isolated XDG config/state/cache directories:

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/bare-cache-native
.venv/bin/python tests/benchmark_cached_bare_gui.py \
  --native-path /tmp/bare-cache-native/target/release/lib_gui_native.so \
  --frames 300 --warm 30 --output /tmp/minimal-cache.json
.venv/bin/python tests/benchmark_gui_full_cache.py \
  --native-path /tmp/bare-cache-native/target/release/lib_gui_native.so \
  --scenario full --calibration live --frames 200 --warm 30 \
  --output /tmp/full-cache.json
```

Use scenarios `full`, `leaf`, `hot` and `recompose`; repeat with another `--seed`.
Use `--calibration calibrated` for completed operation probes and a separate
scene collecting every view's replay observation before the timed comparison.
All variants then share the same frozen measured costs and view observations
while automatic selection continues to run normally. `--calibration frozen`
keeps conservative defaults instead. `--commands 16` increases packet size.
The setup-only calibration GPU waits are never added to the runtime path.

Raw samples are in `/tmp/codex-cached-bare-gui/` and `/tmp/codex-gui-full-cache/`.
