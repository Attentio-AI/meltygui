# Cache-only GUI profiling rerun

November 7, 2026, after the shared-context DrawCapture changes. CPython 3.12,
Linux GLFW/OpenGL, NVIDIA RTX 4090, meltygui-imgui 2.0.4. The native experiment
was rebuilt from current source into `/tmp/codex-cache-rerun-native`; the installed
extension was not replaced. Results are in `/tmp/codex-cache-rerun`.

## Main finding

Removing dependency injection while retaining the full production cache saves
about **0.652 ms, or 5.2%**, on the 341-view full-redraw workload. The stripped
minimal cache remains much faster, but omits layout, input routing, dynamic
declarations and lifecycle features. Its timing is not the cost of production
caching with injection removed.

Two randomized frame-interleaved runs per comparison, seeds 1027 and 2027,
30 warmup and 300 measured frames. Every redraw executes the same 341 bodies,
one rectangle each, with no artificial computation. Values below are mean CPU
submission milliseconds; combined means average the two equal-sized runs.

| Production-cache comparison | Run 1027 | Run 2027 | Combined |
| --- | ---: | ---: | ---: |
| Raw drawing | 0.263 | 0.259 | 0.261 |
| Bare Rust forwarding | 0.377 | 0.373 | 0.375 |
| Cache-only annotation | 12.111 | 11.673 | **11.892** |
| Full @gui | 12.752 | 12.336 | **12.544** |

Paired mean savings are 0.641 and 0.663 ms. Paired median savings are 0.708 and
0.548 ms. Both hosted variants use normal host input, native layout, automatic
cache selection and live calibration. The annotation inherits production
execution, capture, input, policy, composition and lifetime management unchanged.
Only the native binder forwards explicit arguments rather than injecting them.

| Separate minimal-cache comparison | Run 1027 | Run 2027 | Combined |
| --- | ---: | ---: | ---: |
| Minimal cache, full redraw | 2.130 | 2.092 | **2.111** |
| Minimal cache, packet recomposition without bodies | 0.292 | 0.282 | 0.287 |
| Minimal cache, unchanged raster | 0.091 | 0.090 | 0.091 |
| Full @gui, full redraw | 12.525 | 12.027 | 12.276 |
| Full @gui, unchanged replay | 0.398 | 0.385 | 0.392 |

The minimal-cache benchmark disables live calibration for its full-GUI control,
unlike the annotation comparison. Do not combine the two full-GUI controls as
one matched experiment. Cache hits execute zero bodies; full redraw executes 341.

Earlier records were approximately 15.97/16.79 ms for annotation/full GUI and
3.08 ms for minimal redraw. Current absolute times are lower, but this rerun
does not isolate each intervening change. In particular, historical input-apply
ablations refer to copying IO into separate per-view ImGui contexts. Current
capture uses one host context with 341 persistent capture buffers; those old
input-copy savings cannot be presented as current optimization headroom.

## Function profile

A separate 100-frame run after 30 warm frames collected one cProfile per
variant. CPU submission becomes 26.07/26.50 ms for annotation/full GUI under
instrumentation. Use this profile for call counts and hotspot ranking, not
uninstrumented subsystem timings. Cumulative intervals overlap and must not be
added. Native work can be charged to its Python caller.

| Annotation profile boundary | Calls/frame | Self ms/frame | Cumulative ms/frame |
| --- | ---: | ---: | ---: |
| Retained declaration wrapper | 341 | 4.032 | 24.664 |
| Retained `_execute` | 341 | 3.845 | 23.204 |
| Cache-only native call wrapper | 341 | 1.596 | 23.152 |
| Layout commit | 341 | 0.648 | 2.873 |
| Native geometry commit bridge | 342 | 0.539 | 1.476 |
| NativeState configure | 341 | 0.541 | 0.541 |
| Layout begin | 341 | 0.470 | 0.907 |
| Raster selection | 3 | 0.488 | 1.257 |
| Capture residency accounting | 341 | 0.459 | 0.823 |

Declaration reconciliation and execution orchestration remain the largest self
costs. Fixed geometry still triggers 342 native commits per frame. Declaration
work and redundant geometry transactions are stronger investigation targets
than removing injection alone. These measurements do not establish that any
of those operations can safely be skipped for changing real applications.

## Verification and reproduction

All five benchmark runs passed their pixel, body-count/checksum and identity
checks. The annotation comparison retains one ImGui context, 341 captures and
records, one raster, 35,464 packet bytes, 262,144 raster bytes and a 262,144-byte
scratch target. Both hosted variants perform 112,871 packet uploads, 331 raster
refreshes and 63,259 policy evaluations including warmup/mount. No promotions
occur. Live calibration is enabled but operation costs remain conservative
priors; this is not a completed-calibration comparison.

GPU medians are 23.552 microseconds for both hosted variants in run 1027;
run 2027 records 29.696/28.672 microseconds for annotation/full GUI. GPU waits
are outside CPU submission timing. Pixels are checked at mount/end, not every
frame. The isolated native build passes 28 focused cache-only, minimal-cache,
public-GUI and cache-policy tests. Coverage is desktop OpenGL on a reserved
agent desktop, not CUDA, macOS/iOS, full Wayland presentation or release wheels.

Run on a reserved desktop with isolated XDG config/state/cache directories:

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/cache-rerun-native
.venv/bin/python tests/benchmark_cache_annotation.py \
  --native-path /tmp/cache-rerun-native/target/release/lib_gui_native.so \
  --frames 300 --warm 30 --seed 1027 --output /tmp/cache-rerun/annotation-1027.json
.venv/bin/python tests/benchmark_cached_bare_gui.py \
  --native-path /tmp/cache-rerun-native/target/release/lib_gui_native.so \
  --frames 300 --warm 30 --seed 1027 --output /tmp/cache-rerun/minimal-1027.json
.venv/bin/python tests/benchmark_cache_annotation.py \
  --native-path /tmp/cache-rerun-native/target/release/lib_gui_native.so \
  --frames 100 --warm 30 --profile --output /tmp/cache-rerun/profile.json
```

Repeat the two uninstrumented commands with seed 2027 and distinct output names.
The profile JSON contains per-frame function statistics; sibling `.prof` files
contain the call graphs. The benchmark now supports `--profile` for this purpose.
