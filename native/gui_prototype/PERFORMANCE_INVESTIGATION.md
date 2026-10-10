# Full renderer investigation (2026-10-09)

Latest follow-up: [cache optimization with unchanged per-view granularity](CACHE_OPTIMIZATION.md).
The measurements below describe the earlier baseline.

Follow-up: [cache on/off comparison and retained-cache profiles](CACHE_COMPARISON.md)
reruns the full application after the collision fixes, including unchanged frames,
complete invalidation and single-row object invalidation.

Three concrete issues accounted for the crash and much of the full-path
slowdown. They are fixed without disabling caches, widgets, input, collision
handling, independent capture, composition or presentation.

The original 25-row auto-height workload now takes **15.28 ms**, down from
**51.47 ms**. A fresh core-render control takes **25.05 ms**. The corrected
prototype is approximately 1.64x faster in that workload, rather than twice as
slow. The earlier 31–33x headless-wrapper result is still not a full-app speedup.

## Findings and fixes

### Keyboard copying into every view

`ImGuiInput.apply` wrote all 512 keyboard slots into every cached ImGui context,
including unfocused contexts whose entire keyboard state was false. Every
`io.keys_down` property access also constructed a Cython array view. Key mappings
were recopied on every capture too.

In a diagnostic profile of 20 full 25-row redraws, input application consumed
**32.59 ms/frame**. The bridge now samples pressed keys and mappings once per
host frame, writes only changed keys per context, and refreshes mappings when
needed. Focus loss delivers key releases; retirement clears remembered state.
The corresponding profile dropped to **0.93 ms/frame** in input application.

This changes input delivery, not the arbitrary-field dependency-injection
contract. It does not introduce a catalog of framework arguments.

### Texture references across nested ImGui frames

The installed binding shares its texture keepalive list across contexts.
Starting a child's frame clears it while its parent's draw list is unfinished.
Temporary Python texture-ID objects could be freed and their memory reused;
capture then observed a `_DrawList` instead of an integer. Interned small integer
handles masked the problem in small scenes.

The bridge now owns parent references across nested captures and restores them
on return, including exception unwinding. Finalized packets retain their
texture-ID objects until replacement or retirement: an ID object can itself own
an external resource. The regression reserves over 256 GL texture names, submits
a temporary custom texture-ID object, verifies actual pixels, recaptures the
parent, aborts a child capture, and checks reference release on replacement and
close. The capture failure was not bypassed.

### Whole-graph copies during layout commits

Every view commit and placement pass requested `graph.nodes()`, copying and
sorting all IDs before building a Python set just to check whether layout/window
nodes still existed. This caused quadratic scaling even for ordinary collection
widgets without explicit column declarations.

The bridge now checks native membership directly for the actual layout/window
nodes being processed. Collision rules and transaction ordering are unchanged.
In the 300-row profile, enumeration fell from **28,908 calls over 12 frames to
24**, and from **56.96 ms/frame to 0.075 ms/frame**. The remaining two calls per
frame belong to input processing. The unprofiled 300-row redraw fell from
**252.41 to 161.87 ms** after this change alone.

Profile timings include profiler overhead. Application timings below do not.

## Full application results after fixes

For larger comparisons, both root collections receive their window's height
with `--viewport`. This is a separate workload variant: it avoids requesting
one texture taller than 8,192 pixels. Every widget still executes and retains
its own cache. Original auto-height failures remain documented below.

Median milliseconds per forced full redraw, 40 measured frames after 12 warmup
frames, separate processes run sequentially:

| Rows | Core render | Full public @gui | Speedup | Core p95 | @gui p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 25 | 25.44 | 14.86 | 1.71x | 27.09 | 17.09 |
| 100 | 87.37 | 53.55 | 1.63x | 91.00 | 55.74 |
| 300 | 260.95 | 161.87 | 1.61x | 276.28 | 164.88 |
| 1,000 | Not rerun for this variant | Texture budget exceeded | — | — | — |

Every successful full-redraw sample has `4 * rows + 2` collection/widget body
calls. Prototype raster passes are `4 * rows + 3`, including the root. Both
caches remain enabled. Unchanged frames execute zero workload bodies and take
about 8.3 ms on both paths, paced by the 120 Hz desktop; this is not the raw CPU
cost of a cache hit.

Timing covers event polling, the normal surface lifecycle, input, collision
hooks, view work, offscreen capture, postprocessing, buffer swap, housekeeping
and GPU completion via `glFinish`. Tests used the native Wayland backend,
RTX 4090, Ryzen 9 7950X, Python 3.12.3 and a reserved desktop, with isolated
config/session paths. Presentation means the app's swap/wait, not physical
scanout latency. These are steady-state frames after warmup, not startup times.

## Remaining limits and costs

- After the input/lifetime fixes, the original auto-height 100- and 300-row cases
  reach the documented **8,192-pixel cache-height limit** instead of the texture
  crash. The viewport workload does not remove this framework limit.
- The final 1,000-row viewport run reaches the existing **256 MiB texture
  budget**. No budget increase, eviction workaround or cache disabling was used.
- Each cached view still has its own ImGui context, texture, capture and raster
  pass. The final 300-row profile spends **65.63 ms/frame** inside native texture
  replay calls. That includes driver work/waits, not just GPU execution.
- Replay recreates its shared scratch target when dimensions change and saves
  and restores GL state per target. Scratch reuse and batching are concrete next
  candidates, not measured fixes here. Python/native bridge calls and per-view
  ImGui frame setup remain material costs too.

The fixes establish a more credible baseline, not production feature parity or
a universal speedup. Large collections still need a policy for bounded capture,
texture memory and offscreen content.

## Verification and reproduction

**173 focused tests passed**, covering prototype input, retained returns and
invalidation, native GL pixels/replay, collision/layout behavior, edge solves
and native collision fallback. In the real Wayland collision lab, Ctrl+A/text
entry, a button click and a 50-pixel divider drag produced the expected text,
click count and geometry. Other desktop/GPU backends were not manually tested.

Build with `.venv/bin/python tools/build_gui_prototype.py`; the native graph now
exposes `contains`. On a reserved desktop, run each backend sequentially:

```sh
.venv/bin/python tests/benchmark_gui_full_app.py --variant python --rows 300 --viewport --warm 12 --frames 40 --out /tmp/core-300.json
.venv/bin/python tests/benchmark_gui_full_app.py --variant rust --rows 300 --viewport --warm 12 --frames 40 --out /tmp/gui-300.json
```

Omit `--viewport` for the original content-driven-height workload.
`--profile /tmp/gui.prof` profiles measured redraws; keep profiled timings
separate from the comparison. Raw final runs, failure reasons and profile
summaries are in `investigation_results_2026-10-09.json`.
