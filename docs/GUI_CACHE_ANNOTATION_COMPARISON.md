# Full-cache annotation versus @gui: the same 341-view redraw

For a rerun after the shared-context capture changes, see
[current cache-only profiling](GUI_CACHE_PROFILE_RERUN.md). The timings below
describe the earlier implementation and are retained as historical results.

November 7, 2026, CPython 3.12, Linux GLFW/OpenGL, RTX 4090. Isolated experiment;
no installed renderer or public package API was changed.

## Result

Two randomized frame-interleaved runs, each with 30 warm and 300 measured frames.
Every variant draws the same 341-view tree, one rectangle per view, no artificial
computation. The hosted paths use native layout, idle host input, production
automatic caching and live calibration. All frames force all 341 bodies to redraw.

| Path | Mean CPU ms, run 1 | Mean CPU ms, run 2 | Combined mean CPU ms |
| --- | ---: | ---: | ---: |
| Raw draw calls | 0.2520 | 0.2584 | **0.2552** |
| Bare Rust forwarding | 0.3644 | 0.3703 | **0.3673** |
| Cache annotation with full auto-cache/bookkeeping | 15.6628 | 16.2784 | **15.9706** |
| Full @gui | 16.4583 | 17.1188 | **16.7886** |

The paired mean savings of the cache annotation are 0.796/0.840 ms; paired
median savings are 0.846/0.858 ms. Across both runs the annotation saves about
0.818 ms, or 4.9% of the full GUI CPU frame. This is a net comparison against
explicit argument forwarding, not the absolute cost of all argument processing.

The approximately 3 ms minimal cache and 13.9 ms fixed-body experiment are
different implementations. The former omitted production cache bookkeeping;
the latter also bypassed identity/group/measurement scopes. Neither is the
cache annotation measured here. This comparison restores those scopes and is
the relevant answer for the full cache without dependency-aware body dispatch.

## What the annotation does

`cache_only_gui.cache_gui` uses an experimental `CacheOnlyWindow` whose cache
inherits the production `RetainedGui`. The production declaration method is
bound to an alternative native binder. All execution, context management,
invalidation, policy, calibration, capture, composition, eviction, retirement,
input routing and presentation methods are inherited unchanged. The benchmark
asserts method identity for these key paths.

The build helper generates `CacheRenderer` from the current production renderer,
removing frame-global/event injection and the dynamic parameter-resolution loop.
It forwards only explicitly supplied caller arguments to the body, with the input
as its first positional argument. Python handles ordinary function defaults and
argument errors. There is no body signature plan, injected `draw_state`, injected
factory dependency or synchronization of arbitrary resolved parameters into state.
Injection/event-parameter options are rejected explicitly.

Cache geometry NativeState still exists internally. The renderer preserves
nested identity enter/leave, ImGui ID/group scopes, content measurement, None
return normalization, error cleanup, timing/call accounting and geometry state
updates. Declaration-time geometry configuration, defaults and source refresh
are still production code. This experiment is therefore not a claim that all
state, declaration work or Python bookkeeping has been removed.

## Matching work and verification

Both hosted paths retain 341 contexts/records, one raster, 35,464 packet bytes,
262,144 raster bytes and one 262,144-byte scratch target. Including mounting,
both perform 331 raster refreshes, 112,871 packet uploads and 63,259 policy
evaluations per run. No promotions occur in this constantly redrawn tiny-packet
scene. Live calibration remains enabled, gathering 38/39 view observations;
operation costs remain conservative priors because expensive frames are not
eligible for active calibration probes. These are normal live-policy results,
not a completed-calibration study.

GPU elapsed medians match at 29.696 microseconds for both hosted paths and
4.096 microseconds for raw/bare. GPU waits are outside the CPU submission timer;
the two clocks are not added. There are no per-frame pixel readbacks. Exact
initial/final pixels, all body counts/checksums, persistent identities and cache
resource sizes are checked.

Eighteen focused tests pass, covering explicit forwarding without injected
fields, repeated child keys under different parents, independent invalidation,
automatic height measurement, None returns, body-error cleanup, rejected
injection options, public API and cache policy. Coverage is desktop OpenGL;
CUDA, native Wayland presentation, other platforms and release wheels are not
part of this experiment.

## Reproduction

Run graphics commands on a reserved agent desktop with isolated XDG directories.

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/cache-annotation-native
.venv/bin/python tests/benchmark_cache_annotation.py \
  --native-path /tmp/cache-annotation-native/target/release/lib_gui_native.so \
  --frames 300 --warm 30 --seed 1027 --output /tmp/cache-annotation-run-1.json
```

Repeat with seed 2027. The build output includes the generated Rust dispatcher
for inspection. Raw samples, logs and the built experiment are preserved under
`/tmp/codex-cache-annotation/`.

Add `--profile` in a separate run to collect one cProfile per variant, limited
to measured CPU submission frames. JSON includes per-function calls, self time
and cumulative time per frame; sibling `.prof` files retain the complete call
graph. Warmup, GPU waits and pixel checks are excluded. Profiled timing includes
instrumentation overhead and must not replace the ordinary benchmark results.
