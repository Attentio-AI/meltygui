# Complete application benchmark (2026-10-09)

**Historical results before fixes.** The follow-up
[performance investigation](PERFORMANCE_INVESTIGATION.md) fixes the principal
input/layout overhead and texture-reference failure. The original 25-row
workload now takes 15.28 ms versus 25.05 ms for core render. The results below
preserve the initial measurements and failure evidence.

The complete prototype at the time of this first run did **not** deliver the earlier component
benchmark's 31–33x speedup. At 25 rows its full redraw takes **51.47 ms**, versus
**25.57 ms** for core render: approximately **2.01x longer**. At 100, 300 and
1,000 rows the retained prototype fails while capturing a draw list, so those
cases have no valid Rust timing.

## Full redraw results

Median wall milliseconds per complete application frame. Both cache systems
remain enabled; all application view caches receive invalidation each frame.
The first row's count also changes each frame.

| Rows | core_render median | core_render p95 | Public @gui median | Public @gui p95 |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 25.573 | 28.456 | 51.472 | 53.035 |
| 100 | 88.827 | 91.623 | Failed | Failed |
| 300 | 254.027 | 262.155 | Failed | Failed |
| 1,000 | 855.823 | 882.324 | Failed | Failed |

Both successful 25-row variants execute all 102 collection/widget bodies per
redraw, plus the root. The Rust path owns 103 retained nodes and performs 103
offscreen raster passes per redraw. The original no-cache wrapper microbenchmark
did not exercise this machinery. These counts identify substantial additional
work; they do not separately attribute the 51 ms to capture, Python bookkeeping,
context switching, driver overhead or rasterization.

A preliminary 25-row run measured 25.75 ms for core render and 49.44 ms for
the prototype; the longer final run above confirmed the slowdown.

## Unchanged inputs

These are requested frames with unchanged inputs, not an idle app that sleeps
without rendering. The column named `hot` in the raw JSON means this workload;
it does not assert that every cache actually hit.

| Rows | core_render median ms | @gui median ms | Core body executions/frame | @gui body executions/frame |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 8.316 | 8.334 | 0 | 0 |
| 100 | 8.325 | Failed | 0 | — |
| 300 | 243.280 | Failed | 1,153 | — |
| 1,000 | 851.380 | Failed | 3,953 | — |

The desktop runs at 120 Hz and default swap interval remains enabled. The
approximately 8.3 ms results are display-paced, not the raw CPU cost of a cache
hit. The 25-row prototype performs zero offscreen raster passes in this phase.

Core render does not achieve full cache reuse at the larger sizes in this
workload: most bodies continue executing despite unchanged inputs and enabled
caching. That behavior is included in the numbers rather than bypassed.

## Failure in the complete prototype

At 100 rows, two separate attempts failed; the 300- and 1,000-row attempts
failed the same way during initial capture, before completing warmup:

```text
retained_gui_prototype.py, _execute:
    int(command.texture_id)
TypeError: int() argument must be a string, a bytes-like object or a real number,
not 'meltygui_imgui.core._DrawList'
```

The installed ImGui binding stores texture IDs as Python-object pointers.
Its `_ImGuiContext._keepalive_cache` is shared across contexts (confirmed by
creating two contexts and checking object identity), and `new_frame()` clears
that list. Nested retained views start independent ImGui frames while parent
draw lists are still being assembled. The prototype submits temporary integer
objects from `self.gpu.texture(node)` to those lists.

This is strong evidence of a texture-reference lifetime problem in the nested
context integration. Small interned integer handles can mask such a problem.
No fix or workaround was applied to obtain benchmark numbers; a corrected
implementation needs a separate validation and rerun. The raw artifact includes
the failure tracebacks.

## What was timed

- Actual standalone windows on a reserved agent desktop, using the public
  `@os_window`/`@gui` and `@glfw_window`/`@render_func` entry points.
- The normal app loop, OS event polling, surface activation, input processing,
  frame setup, window collision hooks, dynamic injection, IDs, layout and sizes,
  the full cache implementations, ImGui contexts and widgets, draw-list capture,
  GPU rendering, normal postprocessing/composition, buffer swaps and housekeeping.
- A `glFinish()` after each surface frame ensures GPU work is completed rather
  than only submitted. No render stages, caches or default swap pacing were
  disabled. This synchronization is included in the elapsed time.

The timer spans successive app-loop event-poll entries, not a selected inner
function. Benchmark result serialization is outside the measured interval.
These are steady-state frame measurements after warmup, not application startup
measurements. Presentation includes the app's swap call and its wait; it is not
a measurement of physical display scanout latency.

The workload uses the same Python collection/button/integer/float bodies from
`make_views`, with a cached boundary on every decorated view. Collections use
content-driven height on both paths. Both windows were visually checked: they
display the expected widgets and data, although normal backend spacing and
styling differ. The windows start at 1000x800; the reported framebuffer is
1256x1056 including the backend's surrounding area. This exercises the complete
stack for a collection workload, not every possible interaction: no divider
drag, native resize or widget-input gesture is synthesized here.

Machine: Ryzen 9 7950X, RTX 4090, Python 3.12.3, native Wayland window backend,
120 Hz reserved desktop. Processes ran sequentially with separate app IDs and
isolated config/cache paths. Other desktop sessions were left alone.

25-row results use 100 measured frames after 20 warmup frames; larger completed
runs use 40 measured frames after 12 warmup frames. The benchmark checks data
identity; analysis also verified enabled caches and exactly `4 * rows + 2`
collection/widget body executions on every measured full-redraw frame.

## Reproduce

Run on a reserved desktop with the usual desktop launcher, one process at a time:

```sh
XDG_CONFIG_HOME=/tmp/melty-full-benchmark/config XDG_CACHE_HOME=/tmp/melty-full-benchmark/cache \
  .venv/bin/python tests/benchmark_gui_full_app.py --variant python --rows 25 --warm 20 --frames 100 --out /tmp/full-python-25.json
XDG_CONFIG_HOME=/tmp/melty-full-benchmark/config XDG_CACHE_HOME=/tmp/melty-full-benchmark/cache \
  .venv/bin/python tests/benchmark_gui_full_app.py --variant rust --rows 25 --warm 20 --frames 100 --out /tmp/full-rust-25.json
```

Repeat with `--rows 100`, `300`, or `1000`. Failed runs exit nonzero without
producing successful timing JSON. `--inspect-seconds 6` pauses the first warmup
frame for visual inspection; that pause is not part of the measured samples.

Raw measurements and failures:
`full_app_benchmark_results_2026-10-09.json`.
