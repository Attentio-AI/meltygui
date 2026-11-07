# Packet and retained-bookkeeping implementation results

Implemented later on 2026-11-07, following [the phase breakdown](GUI_PHASE_BREAKDOWN.md)
and [isolated dispatch experiments](GUI_DISPATCH_EXPERIMENTS.md). This report
supersedes their statement that production was unchanged; their original
measurements remain historical evidence and have not been rewritten.

## Production changes

### Safe packet byte extraction

`TextureCache.add_packet` now recognizes typed Python bytes and bulk-copies
their contents into owned Rust vectors. Other sequence inputs retain their
existing extraction behavior. Actual production capture and test callers
supply bytes; no caller changes or raw-pointer API were needed.

The old PyO3 `Vec<u8>` signature iterated each byte before entering the Rust
function. The new method avoids that conversion loop. A private Rust
`store_packet` helper retains the original validation and packet accounting.
Malformed lengths/commands are rejected before residency changes. Packet
buffers remain owned; texture-ID owners and nested parent keepalive restoration
are unchanged. There are no new GPU reads, waits, probes or rendering passes.

The normal extension was built and atomically installed with
`tools/build_gui_prototype.py`. A copy of the ordinary pre-change extension
and its source was saved before editing, providing the old-conversion control.

### Less repeated retained bookkeeping

The Python retained path now:

* Leaves already-registered identity aliases alone when a non-path caller
  supplies the same input object. String/path-like aliases are still recomputed.
* Registers returned state on first execution, including a previously culled
  declaration, then skips repeated association of the same state object.
  State assignment remains before association as in the original ordering.
* Uses `invocation`'s canonical height/None marker and one `rect` query instead
  of materializing a nine-field `info` dictionary. Invocation still runs before
  the geometry sync. Sync updates indices/invalidation, not the state dimensions
  or auto-height marker used here.

The final implementation uses **existing native APIs**. A slightly faster
fused native accessor was tested but not required: newly hotswapped Python must
also work with already-loaded older native graph instances. The final Python
candidate passed its focused tests against the saved old extension.

## Packet measurements: before the bookkeeping change

CPython 3.12, Ryzen 9 7950X, NVIDIA RTX 4090. Each run has 24 warm and 150
measured frames, identical draw/checksum work, frozen conservative cache costs
and disabled calibration/probes. Runs follow old/new/new/old order. The native
layout bridge is attached; the indicated cases also run idle host input.
Raw reference samples are retained in every result. The Python source is held
at its pre-bookkeeping version for these comparisons.

| Full-redraw workload | Old CPU medians ms | Typed-byte CPU medians ms |
| --- | ---: | ---: |
| One rectangle per view, no host input | 14.247 / 14.403 | 14.735 / 14.115 |
| One rectangle per view, idle host input | 17.262 / 18.465 | 17.966 / 16.485 |
| Sixteen rectangles per view, idle host input | 22.291 / 22.706 | **18.760 / 19.146** |

The sixteen-rectangle case shows a repeatable approximately **3.5 ms** reduction
(about 15-16% in these runs). Its old p95s are 25.241/25.243 ms and new p95s
19.881/20.971 ms. The trivial packet case is within workstation variation;
no small-case speedup is claimed from these runs. This is consistent with a
payload-dependent conversion improvement, not a fixed per-view gain.

Additional matched pairs, with idle host input:

| Control | Old CPU median ms | Typed-byte CPU median ms |
| --- | ---: | ---: |
| Hot replay, zero bodies | 0.331 | 0.330 |
| 512 integer operations per body | 28.595 | 27.077 |
| Fully predeclared culled tree, five bodies | 0.768 | 0.738 |

Those single pairs retain their samples/p95s but are not treated as stable
percentage improvements. The main scene always executes 341 bodies with
checksum 58,311. The heavy checksum is 733,500,616,135; the culled checksum is
15. Pixels match raw exactly. Contexts, IDs, raster/scratch budgets and packet
bytes match: 35,464 bytes for one rectangle and 567,424 for sixteen. Repeated
rectangles may merge into one ImGui command; these are primitive/payload counts,
not sixteen independent draw commands.

CPU submission and GPU elapsed intervals are recorded separately. No GPU
speedup is claimed for the CPU ingress optimization. Cold mounts are separate
single samples in each JSON result, not mixed into warmed medians.
One-rectangle GPU medians are 17.408 us in both old and new runs. Sixteen-rectangle
intervals are 29.696/41.984 us old and 24.576/26.112 us new; submission gaps and
driver scheduling can affect these intervals. Cold sixteen-rectangle mounts
are 47.21/46.32 ms old and 47.59/46.33 ms new, with no clear cold-start gain.

## Incremental bookkeeping measurements

These are separate randomized **frame-interleaved** comparisons after the
packet implementation, with real native layout and idle host input. Do not
add their percentages to packet results from other runs/workloads.

The final ABI-compatible implementation uses 24 warm and 200 measured frames:

| After packet implementation | CPU median ms | p95 ms | MAD ms |
| --- | ---: | ---: | ---: |
| Original bookkeeping control | 18.320 | 21.326 | 0.471 |
| Final bookkeeping implementation | **17.762** | **20.707** | 0.474 |

The median reduction is 0.558 ms, or about **3.0%**. Pairing samples by frame
gives a 0.625 ms median saving, with the candidate faster in 77.5% of frames.
Hot replay is 0.428/0.426 ms. The matched raw control is 0.273 ms. Pixels,
341 bodies/checksum, contexts/IDs and packet/raster/scratch accounting are equal.
GPU medians are 29.696 us for both. Single cold samples are 39.454/38.720 ms;
they are not cold-start distributions.

Earlier repetitions of the combined alias optimization plus an isolated fused
metadata accessor measured 18.427→17.599 ms and 18.552→17.706 ms. An existing-API
three-query variant measured 18.784→18.043 ms, versus 17.980 ms for the fused
accessor in that same run. The final two-query implementation avoids a new
required native method. These earlier variants are supporting experiments,
not additional production gains to accumulate.

## Host-commit experiment: not adopted

An isolated candidate adds O(1) topology and **position** revisions to native
edge graphs, rather than misusing topology version as a position generation.
It resets commit eligibility at every native observation so the first commit
still acknowledges/consumes the host frame. It also accounts for root frame
and invocation replacement, adapter identity and graph replacement on rollback.

In two frame-interleaved runs, host coalescing alone changes 18.427→18.344 ms
and 18.552→18.426 ms. The root snapshots and guard calls consume most of the
saved commit work. This small gain does not justify the added production
state/generation complexity. **Neither the host guard nor its native revision
API was promoted.**

The populated-host-layout control retains real observations and native commits
for every redraw. With 1/16/64 layouts, one-leaf redraw medians were
37.3/344.0/1337.8 us for control and 37.9/325.9/1301.2 us for the host candidate.
The bookkeeping prototype gave 37.5/341.5/1361.4 us. These small/noisy differences
do not establish an improvement for populated layouts. Global snapshot and
application work remains an unresolved coupling; the optimized alias/metadata
path is not a solution to that scaling problem.

## Verification and limits

The installed final implementation passes **306 relevant tests**, with two
native-revision-only experimental tests skipped because that API was not
promoted. The final Python candidate also passed **132 tests against the saved
old native extension**, with the same two experiment-only skips.

Coverage includes packet alpha/clipping/GL-state restoration, resource budgets,
texture ownership, widgets/focus, scrolling/positions, cache-mode transitions,
auto-height/layout/rollback, default/state injection, first and repeated state
aliases, first execution after culling, dynamic path aliases, replacement-input
cleanup, pending scalar returns, persistence and Python source hotswap. New
packet tests cover bytes, bytearray, lists and memoryviews, verify owned data
after caller mutation/release, and verify malformed input leaves packet stats
unchanged.

GPU verification is Linux GLFW/OpenGL on NVIDIA. CUDA, macOS, Windows, complete
native Wayland presentation, native-module live reload and a noneditable wheel
were not validated in this task. Existing loaded native mappings are preserved
by atomic installation; the Python change introduces no new native requirement.

The remaining gap is still large. Declaration and execution orchestration,
per-view IO/lifecycle work and populated-layout snapshots/application remain
the next targets. The host experiment shows that merely adding a guard can
replace one cost with another; future changes need a cheaper ownership-level
change signal rather than per-call reconstruction of guard inputs.

## Reproduction and evidence

All graphics commands require a reserved agent desktop and isolated session
paths. Artifacts are under `/tmp/astra-gui-implementation/` and in the uploaded
archive. `packet-retained_gui_prototype.py` is the archived pre-bookkeeping
Python source used for controls; old native source and both build logs are
included. No instrumentation is enabled in performance runs or production.

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/pytest -q tests/test_gui_*.py tests/test_retained_gui_*.py

# Repeat the incremental bookkeeping comparison against the archived control.
.venv/bin/python tests/compare_gui_followups.py \
  --native-path /tmp/astra-gui-implementation/packet-native.so \
  --python-baseline /tmp/astra-gui-implementation/packet-retained_gui_prototype.py \
  --candidates control,bookkeeping --native-layout --host-input \
  --frames 200 --warm 24 --output /tmp/bookkeeping-comparison.json

# Hold Python at the pre-bookkeeping version for a packet-only comparison.
# Run once with baseline-native.so and once with packet-native.so (ABBA recommended).
.venv/bin/python tests/gui_followup_experiments.py --candidate control \
  --native-path /tmp/astra-gui-implementation/baseline-native.so \
  --python-baseline /tmp/astra-gui-implementation/packet-retained_gui_prototype.py \
  --target profile_gui_phase_taxonomy.py -- --host-input --commands 16 \
  --measure-raw --frames 150 --warm 24 --output /tmp/old-packet.json
```
