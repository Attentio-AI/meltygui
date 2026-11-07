# Bare forwarding wrapper versus retained @gui

Measured November 7, 2026 on CPython 3.12, Linux GLFW/OpenGL and RTX 4090.
This experiment does not change the installed renderer or public `@gui`.

## Question and scope

How much time does a decorator take when it only forwards a call, with no
parameter matching or draw state? `tests/benchmark_bare_gui.py` compares:

* Raw: call the Python drawing function directly.
* Bare Python: a closure forwards `input_value` and explicit `**kwargs`.
* Bare Rust: a PyO3 callable forwards the same input and keyword dictionary to
  Python. It has no runtime, parameter plan, NativeState or draw-state object.
* Full GUI: the current retained `@gui`, forced to redraw, with the actual native
  layout bridge attached to a fixed surface adapter and idle host input enabled.

All paths use the same recursive body from `benchmark_gui_overhead.Scene`,
341 nodes, explicit geometry arguments, one rectangle per node and the same
checksum. Bare paths draw into the shared ImGui draw list. Full GUI retains its
normal per-view lifecycle, graph, layout, input, packet capture and composition.
The optional `wrapper` argument in the existing benchmark Scene affects only
the raw branch and defaults to its original behavior.

**This is not an injection-only ablation.** The full-to-bare difference includes
all those retained services, not just parameter matching and draw state. It
establishes the forwarding-only cost; it does not measure the fraction of
full GUI time specifically caused by dependency injection. Python still binds
function arguments normally in every variant.

## Results

Each workload ran twice with different randomized frame-interleaved orders,
30 warm frames and 300 measured frames per variant per run. Values below are
arithmetic mean CPU submission milliseconds, not medians or display latency.

| Path | Simple drawing, run 1 / run 2 | 512 integer operations per body, run 1 / run 2 |
| --- | ---: | ---: |
| Raw | 0.2491 / 0.2496 | 9.3450 / 9.3075 |
| Bare Python | 0.3361 / 0.3332 | 9.3975 / 9.3528 |
| Bare Rust | 0.3599 / 0.3600 | 9.8111 / 9.6146 |
| Full retained GUI | 15.7905 / 15.6389 | 26.4731 / 26.3449 |

For simple drawing, Python forwarding adds 0.084-0.087 ms across 341 calls,
about 0.25 microseconds per call. Rust forwarding adds about 0.110-0.111 ms,
about 0.32 microseconds per call. The extra Python-to-Rust-to-Python boundary
is not faster than the plain Python closure in this test.

Forwarding accounts for less than 1% of the full GUI overhead above raw in
the simple scene. The remainder is not attributed to any individual service
by this experiment. The heavier workload shows larger timing variation and
should not be used to infer precise per-call forwarding costs.

CPU submission includes scene traversal, host work where applicable, and the
common outer ImGui render/submission. Outer frame begin is outside that CPU
timer, matching the existing comparison harness. GPU elapsed queries and
their waits are outside the CPU timer. GPU and CPU durations are not added.
Pixel readbacks occur only at initial/final validation checkpoints.

## Verification and reproduction

All four runs passed exact initial/final raw-pixel comparisons, per-frame body
count/checksum checks, and full-GUI state/context/unique-ID stability checks.
Both wrappers passed checks for input/return identity, keyword forwarding,
unknown-argument errors, exception identity, recursion and GC closure-cycle
cleanup. The focused public API suite passed 8 tests. No CUDA, native Wayland
presentation, other operating system, installed wheel or release was tested.

Reserve an agent desktop and use isolated XDG config/state/cache directories.
Build once into a new destination; this never overwrites the installed module:

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/bare-gui-native
.venv/bin/python tests/benchmark_bare_gui.py \
  --native-path /tmp/bare-gui-native/target/release/lib_gui_native.so \
  --frames 300 --warm 30 --seed 1027 --output /tmp/bare-gui-simple.json
```

Repeat with `--seed 2027`. Add `--work 512` for the computation-heavy scene.
The JSON includes individual CPU/GPU samples, mean/median/p95/MAD statistics,
paired differences against raw, renderer identity and validation flags.
Original results and build logs are in `/tmp/codex-bare-gui/`.
