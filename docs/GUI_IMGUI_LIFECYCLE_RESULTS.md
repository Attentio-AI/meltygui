# Fused retained ImGui lifecycle

Measured 2026-11-07 on CPython 3.12, Linux GLFW/OpenGL, Ryzen 9 7950X and
RTX 4090. This optimizes the active Rust GUI prototype's Python/native bridge.
It does not enable the isolated Rust declaration or record-storage experiments.

## Change and preserved behavior

The maintained `meltygui-imgui` binding now supplies `begin_retained_frame(width,
height)` and `end_retained_frame()`. They execute the existing NewFrame, fixed
retained-window setup, cursor/clip setup, clip removal, End and Render sequence
directly in C++. The Python runtime makes two calls instead of repeatedly
looking up and invoking the individual bindings and constructing temporary
window/draw-list wrappers. It also retrieves the setup IO wrapper once.

Each view retains its own context and widget state. Input delivery still occurs
before NewFrame; input status is consumed before End/Render. Parent texture
keepalives are still saved/restored around nested captures, and new frames clear
the binding's keepalive list exactly as before. There are no new GPU operations,
readbacks, waits, framebuffer allocations or retained drawables. Dear ImGui's
actual NewFrame processing is not skipped.

The source implementation spans this repository and the sibling
`meltygui-imgui` repository (`core.pyx`, generated `core.cpp`, and `core.pyi`).
This opt-in prototype currently requires a binding built with those helpers.
Published 2.0.3 bindings and already-loaded older bindings do not contain them.
The local wheel was rebuilt and installed for validation; no package was
published. A coordinated binding version and dependency update is required
before releasing this integration. No silent old-binding fallback was added.

## Matched measurements

Each comparison uses 30 warm and 250 measured frames, randomized frame order,
341 rectangle bodies, checksum 58,311, real native layout with a fixed surface
adapter, frozen cache costs and disabled calibration. Both implementations run
against the same rebuilt ImGui binding. The control restores the pre-change
`_context` and `_execute` methods; other bookkeeping is identical.

| CPU submission | Control median ms | Fused median ms | Paired median saving ms | Frames faster |
| --- | ---: | ---: | ---: | ---: |
| Full redraw, run 1 | 13.983 | 13.614 | 0.333 | 79.2% |
| Full redraw, repeat | 14.802 | 14.609 | 0.394 | 70.8% |
| Full redraw with idle host input | 17.720 | 17.195 | 0.598 | 73.2% |

The difference of full-redraw medians is 0.369/0.193 ms (2.6%/1.3%). Run-to-run
workstation variation is substantial; the paired results support a modest
improvement, not a multi-millisecond reduction. Full-redraw p95s are
15.387/15.311 ms and 17.555/17.062 ms, control/fused respectively.

Separate sparse lifecycle probes (30 warm, 200 measured frames) measure setup
at 1.919 to 1.619 ms and finalization excluding input finish at 0.303 to 0.265 ms.
Together these intervals fall from 2.222 to 1.884 ms, about 15.2%. These are
instrumented means, not additive allocations of the uninstrumented frame.

GPU medians are 29.184/28.672 us and 27.136/27.648 us for the two main comparisons,
and 30.720/30.720 us with host input. No GPU improvement is claimed. Hot replay
executes no lifecycle and is not expected to improve.

## Verification and reproduction

121 focused tests passed, covering exact packet parity, fractional geometry,
text/widgets, input/focus/clipboard, nested texture ownership, cache eviction,
scroll/layout, state and persistence. The new lifecycle test compares complete
vertex/index buffers and draw commands against the ordinary API sequence.
Matched timing runs check pixels, body counts/checksums, state/context identity
and resource counts. The noneditable binding wheel also passed a smoke test
outside the checkout.

Coverage is CPython 3.12 GLFW/OpenGL. Full native Wayland presentation, Windows,
iOS, CUDA and live upgrade of an already-loaded binding were not verified.

Save the pre-change retained Python module before applying the change, build
the matching `meltygui-imgui` wheel, then run on a reserved agent desktop:

```sh
.venv/bin/python tests/compare_gui_lifecycle.py \
  --python-baseline /path/to/baseline_retained.py --native-layout \
  --warm 30 --frames 250 --output /tmp/lifecycle-comparison.json
```

Repeat with `--host-input`. Add `--commands 16`, `--work 512` or `--culled` for
the existing workload controls; these extra workloads were not measured here.
The original samples, saved control and runner are in
`/tmp/gui-lifecycle-optimization` on the development machine.
