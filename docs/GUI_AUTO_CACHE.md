# Automatic raster selection for `@gui`

`Toggles.Rust.auto_cache = True` is the default for the Rust GUI prototype.
The normal retained representation is a drawing packet. Small child views share
their enclosing presentation pass; they do not acquire their own pixel storage
or framebuffer merely because they have `@gui`.

`use_cache=False` keeps its existing immediate-body opt-out. `use_cache=True`
means normal retained-policy eligibility, not a forced texture. Setting the
single global toggle to `False` restores the per-view raster baseline. Existing
settings machinery supplies the new nested default; no separate configuration
file, environment switch or per-view policy option is introduced.

## Findings in brief

Automatic selection removes most of the per-view raster overhead, but does not
make full redraw comparable to raw Python drawing yet. In the matched 341-view
case, CPU submission falls from 20.732 ms to about 14.3 ms; raw drawing is about
0.30 ms. Raster boundaries fall from 341 to one, and the GL interval falls from
5.542 ms to roughly 0.02 ms. The remaining work is principally declaration,
execution orchestration, ImGui setup and packet capture.

Conservative defaults and calibrated coefficients choose the same boundaries
in the measured workloads. Their small timing differences do not establish a
calibration speedup. Both promote a reusable expensive single-command fill;
neither promotes the continuously dirty version. That dirty case is slightly
slower under shared replay, so automatic mode is not a universal performance win.

These results replace neither the original benchmark nor its workload: the
tables below identify the matched rerun, separate sequential controls and
instrumented attribution. Calibration/probe work is excluded from timed policy
comparisons. No CPU and GPU columns should be added together as frame latency.

## Code update map

| Component | Change and responsibility |
| --- | --- |
| [toggles.py](../meltygui/core/runtime/toggles.py) | Adds the default `Toggles.Rust.auto_cache = True` through the existing settings hierarchy. |
| [gui_cache_policy.py](../meltygui/core/rendering/gui_cache_policy.py) | Owns the bounded cost/reuse decision, hysteresis and independent-clock regression guard. It does not discover hardware or manage GL objects. |
| [retained_gui_prototype.py](../meltygui/core/rendering/retained_gui_prototype.py) | Connects policy to affected views, handles live global-mode transitions, keeps mandatory boundaries, tracks residency, and requests composition without rerunning bodies for representation changes. |
| [texture.rs](../native/gui_prototype/src/texture.rs) | Separates stable logical image references from optional raster storage; supplies promotion/demotion, workload counters, sampling and resource accounting. |
| [shared_replay.rs](../native/gui_prototype/src/shared_replay.rs) | Expands retained child references in drawing order with local translation and clipping into the nearest raster ancestor's shared upload. |
| [retained.rs](../native/gui_prototype/src/retained.rs) | Supplies composition scheduling and geometry/visibility relationships independently of body execution. |
| [gui_calibration.py](../meltygui/core/rendering/gui_calibration.py) and [native sampler](../native/gui_prototype/src/calibration.rs) | Supply immutable device-specific cost estimates and bounded asynchronous observations. Environment and profile rules remain behind the calibration boundary. |
| [policy tests](../tests/test_gui_cache_policy.py) and [GPU tests](../tests/test_gui_auto_cache_gpu.py) | Check decisions, transitions, rendering, input/state continuity and failure handling. |
| [overhead benchmark](../tests/benchmark_gui_overhead.py) and [selection benchmark](../tests/benchmark_gui_cache_selection.py) | Compare raw, per-view raster, conservative automatic and calibrated automatic paths with matched work and pixel checks. |

The earlier eviction-trigger and lazy-acknowledgement improvements remain in
place; see [their experiments](GUI_OPTIMIZATION_EXPERIMENTS.md). The isolated
quad-index-upload experiment was not independently promoted into the normal
renderer. It is distinct from this shared-replay architecture change.

The implemented control flow is approximately:

```python
# Pseudocode, not runnable code.
retain_identity_state_context_and_commands(view)
if required_presentation_boundary(view) or not Toggles.Rust.auto_cache:
    keep_raster(view)
else:
    observe_visual_refreshes_and_actual_replay(view)
    if sustained_savings_with_reuse(view, costs) and budget_allows(view):
        promote(view)
        remove_redundant_descendant_rasters(view)
    elif consistently_unhelpful(view):
        demote_to_shared_commands(view)
replay_in_original_order()
```

For diagnosis or a per-view texture comparison, use the full global setting:

```python
from meltygui.core.runtime.toggles import Toggles

Toggles.Rust.auto_cache = False  # Per-view raster baseline.
Toggles.Rust.auto_cache = True   # Restore normal automatic selection.
```

Views require no new arguments. `use_cache=False` is an explicit immediate-mode
opt-out, not the internal implementation of an automatic demotion.

## Representation and lifetime

Identity, native state, declarations, geometry, visibility, invalidation,
ImGui contexts, input focus/capture, callback ownership and drawing packets
remain independent of optional raster storage. A logical child reference reserves
a GL name without instantiating pixel storage. Promotion/demotion preserves that
name, so already-retained parent packets need no Python recapture or ID change.

Host-presented roots and floating portal windows remain mandatory raster
boundaries: the host ImGui draw list presents their straight-alpha textures.
Other views expand their retained packets into the nearest raster ancestor's
shared upload. Expansion preserves parent commands before and after child
references, nested clipping and the existing snapped placement rule. Scroll
children expand in the existing local visibility order. Packet replay never
executes the Python body again.

A shared pass accumulates premultiplied colors and resolves once at its raster
boundary. The previous per-view path rounded intermediate RGBA8 textures at each
nesting level. Opaque transition checks are byte exact; tested translucent
combinations differ by at most two channel values out of 255. This difference is
reported explicitly instead of calling the two pipelines universally byte exact.

The native hard texture budget still includes retained raster and bounded scratch
storage. Optional allocation failure keeps a candidate in command form; a required
presentation allocation still fails explicitly. The scroll residency budget now
charges retained packet bytes plus actual raster bytes, with a one-byte minimum
for empty captures so their contexts still count. The tile-count limit continues
to bound empty/offscreen captures. Eviction preserves session state. Promotion,
demotion and global mode switches keep live contexts and focus/capture intact.

## Cost and reuse policy

`gui_cache_policy.py` owns the local decision. It receives at most 64 affected
candidates per composition call from a rotating native queue. Unchanged hot
frames do not scan the graph. A global toggle change is an intentional one-time
representation transition over resident captures; no render bodies execute for it.

A visual refresh is one node in the graph's deduplicated composition set. It
includes body changes, descendant changes, scrolling and child placement changes.
Several descendant edits before a batch count once. A replay is one logical
presentation in a native composition batch. A view's own raster pass and its
parent's cached-reference quad count once, not twice. The policy estimates the
fraction of replay opportunities needing visual refresh with an exponential
average. Continuously changing views are rejected before evaluating cost terms.

Marginal shared replay cost uses retained payload bytes, submitted draw commands,
actual available draw-span observations and the immutable `RetainedGui.costs`
snapshot. A child's estimate excludes the fixed upload shared by the whole pass.
A candidate pays composite cost, its refresh fraction of replay plus transition,
clear and resolve costs, and allocation amortized over 32 opportunities. This is
a bounded prediction horizon, not a promise of future reuse.

Calibrated coefficients require a 25% predicted saving; conservative defaults
require 50%. Two consistent decisions implement promotion/demotion hysteresis.
CPU and GPU clocks are never added. A saving on one clock cannot excuse an
unbounded regression on the other: the other estimated clock must remain within
125% of its previous value or 10% of the dominant observed cost. Unknown GPU work
stays unknown. The policy is a conservative local estimate, not a whole-application
critical-path model.

A selected ancestor covers its inline descendants. Promotion removes redundant
optional descendant rasters; portals retain independent presentation boundaries.
Subsequent descendant edits still refresh the ancestor's visual-change counter,
so a stable Python container around animated content does not appear reusable.

## Actual observations

The asynchronous sampler can select a logical subtree in a shared pass. Its
existing ordered command span is bracketed by timestamp queries on the required
render, with no second execution and no blocking query read. Cached-reference
quad spans are excluded, so their cheap presentation cannot overwrite an
expensive underlying replay estimate. A single ImGui draw command containing
heavy overlapping fill is explicitly tested and measured.

Shared-span observations include draw submission/GPU execution, but exclude the
one shared buffer upload and ancestor clear/resolve; those costs come from their
separate calibrated terms. The legacy raster sampler retains its previous whole
raster interval. Both scopes are documented and must not be conflated.

External shaders, CUDA and work executed before packet replay are not covered.
Their existing `Calibration.observe(..., view=(node, 'external-body'))` seam
remains separate. Such work may already be avoided by retained body/texture
ownership even when the view is shared; adding its time to a raster-promotion
benefit would be incorrect. Arbitrary shader callbacks inside retained ImGui
packets are not introduced by this change.

## Implementation lessons

- **Logical identity cannot depend on storage.** Parent packets previously
  embedded child texture IDs. Stable references now survive allocation changes,
  so promotion does not require recapturing Python bodies or replacing contexts.
- **Python capture counts are not visual-change counts.** A stable container
  can change every frame because a child animates or scrolls. Those changes must
  count toward the cost of refreshing its texture.
- **Reuse counters need a precise denominator.** A raster's own pass and its
  parent reference are one presentation opportunity, not two. Multiple edits
  before the same composition are one visual refresh.
- **A cached-image sample is not the underlying render cost.** Sampling the
  cheap image quad would overwrite the estimate that justified promotion.
  Shared subtree spans provide observations before promotion, and cached-reference
  spans are excluded from that underlying-cost estimate.
- **One clock cannot excuse arbitrary harm to the other.** A GPU saving with a
  much larger CPU penalty is rejected. This is a conservative local guard, not
  a complete model of the application's critical path.
- **Representation changes must preserve input ownership.** Reusing state and
  geometry alone is insufficient: contexts, widget focus and mouse capture also
  have to survive. Transition tests exercise those invariants.
- **RGBA8 boundaries affect rounding.** Removing intermediate resolves can
  change translucent pixels slightly. Exact opaque parity and bounded tested
  translucent differences are reported separately below.
- **Defaults versus calibration require a matched experiment.** The matrix
  freezes both snapshots and stops probes before timing. Different coefficients
  need not produce different choices when a workload is far from break-even.

## Validation and evidence

The focused tests cover default/calibrated decisions, separate-clock regressions,
continuous changes, measured one-command fill, hysteresis, memory fallback,
opaque/translucent pixels, clipping, fractional placement, partial child edits,
mode transitions, stable state/context identity and active text focus. Existing
scroll, widget, layout, retained lifetime and calibration suites cover the shared
path. Per-texture reference-image suites explicitly select the legacy baseline;
the new transition suite compares it against shared output.

`benchmark_gui_overhead.py --graphics --policy-matrix` randomizes legacy,
conservative-default and calibrated groups for each matched tree/workload. The
existing within-group variants remain interleaved. Calibration is completed
before timing and its immutable snapshot is frozen; passive sampling, profile
workers and idle probes are disabled during these comparisons. Cold mount costs,
body/checksum counts, identities, contexts, raster/scratch/packet resources and
CPU/GPU samples are retained. `benchmark_gui_cache_selection.py` separately
compares reusable and continuously dirty one-command GPU fill, using the same
actual first-shared-render observation across the three cost modes.

Benchmark completion waits and calibration probe loops are test-harness code.
They are not part of the application renderer. This coverage is Linux desktop
OpenGL through GLFW/Wayland. It does not establish CUDA, macOS, Windows,
noneditable-wheel or native-module live-reload correctness.

## Measured results, 2026-11-07

CPython 3.12, Ryzen 9 7950X, NVIDIA RTX 4090 desktop OpenGL. The overhead matrix
uses 16 warm and 60 measured frames per mode, identical tree/draw/checksum work,
randomized mode groups and interleaved variants. Values below are CPU submission
medians in milliseconds; GPU columns are elapsed GL intervals in microseconds.
GPU intervals can include submission gaps and driver scheduling, and must not be
added to CPU times.

| 341 visible trivial views | CPU ms | GPU us | Raster boundaries | Raster bytes |
| --- | ---: | ---: | ---: | ---: |
| Raw draw loop, across matched groups | 0.292–0.309 | 2.048–3.072 | 0 | 0 |
| Immediate `use_cache=False` | 1.986–2.063 | 2.048–3.072 | 0 | 0 |
| Global auto off, retained full redraw | 20.732 | 5542.400 | 341 | 1,310,720 |
| Auto on, conservative defaults | 14.422 | 16.384 | 1 | 262,144 |
| Auto on, frozen calibrated costs | 14.291 | 18.944 | 1 | 262,144 |

This is a substantial composition/boundary reduction, **not a solution to the
remaining CPU redraw gap**. The matched reduction is approximately 30–31%, not a
comparison against the inherited 19.034 ms result from a different run. Default
and calibrated modes choose the same single required boundary here: every child
changes, so additional rasters offer no reuse. Both retain all 341 contexts and
341 distinct IDs, execute exactly 341 bodies with checksum 58,311, and match raw
pixels exactly. Packet bytes remain 35,464. Scratch memory falls from 349,184 to
262,144 bytes. Cold mounts are 75.687 ms legacy, 35.684 ms default and 37.030 ms
calibrated; no optional child raster is allocated at startup.

Warm unchanged CPU medians in those groups are 0.162 / 0.145 / 0.142 ms
(legacy/default/calibrated), with zero body executions and zero raster passes.
They are not evidence of beating the earlier separate ~96 us hot measurement;
sequential plain profiling controls below reach ~100 us on this same revision.

| Matched workload, retained redraw | Legacy CPU ms | Defaults CPU ms | Calibrated CPU ms |
| --- | ---: | ---: | ---: |
| 341 visible views, 512 integer operations/body | 29.570 | 23.386 | 23.624 |
| 32-deep all-visible chain, trivial bodies | 1.730 | 1.384 | 1.420 |
| 585 visible views, trivial bodies | 30.672 | 21.526 | 22.040 |
| 341 predeclared, 5 visible, trivial bodies | 0.551 | 0.519 | 0.516 |
| 341 predeclared, 5 visible, 512 operations/body | 0.804 | 0.818 | 0.800 |

The already-mounted culling case continues to execute only five bodies, preserving
341 declarations/IDs. There is no reliable CPU win in its heavier five-body case;
the automatic path is within ordinary run variation. Deep/wide all-visible
scenes use one presentation raster rather than a texture per level/view.

The completed calibration sweep takes 76.97 ms **outside the timed matrix**.
All six CPU operations have nine samples. GPU allocate/transition/resolve/composite
have nine, upload seven and clear six; zero/subresolution intervals are not
invented measurements. The resulting fitted coefficients are frozen for all
calibrated cases. This is small-size local calibration, not a guarantee for every
resolution or workload. Default and calibrated policy choices match throughout
these forced-redraw/hot workloads.

### Actual single-command GPU workload

A second interleaved benchmark uses 1,024 overlapping fill rectangles in one
ImGui draw command. Its first shared render measures 6.011 us CPU draw submission
and 152.576 us GPU work. This actual workload estimate is supplied identically to
the default and calibrated modes; baseline operation coefficients remain distinct.
The benchmark freezes sampling/probes before timing 60 frames after 24 warm frames.

| Single-command workload | Legacy CPU/GPU us | Defaults CPU/GPU us | Calibrated CPU/GPU us |
| --- | ---: | ---: | ---: |
| Stable heavy child, dirty sibling | 227.3 / 20.5 | 234.2 / 13.3 | 235.5 / 13.3 |
| Continuously dirty heavy child | 950.6 / 141.8 | 964.7 / 156.7 | 974.3 / 156.2 |

Both automatic cost modes make the **same** decisions: one promotion for the
stable heavy child (two raster boundaries total), no promotion for the dirty
heavy child (one boundary). Legacy retains three. Stable heavy content executes
once, the dirty child executes on each required frame, and root bodies execute
once. Every mode keeps three IDs and matches pixels exactly. Calibrated selection
does not improve this particular workload over defaults; the dirty heavy case
has slightly higher measured submission/GPU intervals under shared replay. The
policy tests separately establish a calibrated threshold case that changes the
decision, and reject a GPU saving that would cause overwhelming CPU regression.

### Residual profiling

The separate sequential uninstrumented harness measures 17.395 ms legacy versus
13.132 ms automatic full redraw, with raw at 0.215 / 0.211 ms and hot at
99.8 / 102.7 us. The difference from the interleaved matrix is preserved in the
artifacts rather than hidden by selecting the more favorable run.

Phase instrumentation raises redraw to 22.935 / 17.680 ms; cProfile raises the
automatic case to 26.395 ms. Instrumented totals are not production performance.
The phase means attribute composition to 5.276 ms legacy versus 0.931 ms automatic,
including 0.280 ms of policy work in automatic mode. Automatic exclusive costs
still include 3.845 ms declaration work, 4.327 ms execution orchestration,
3.017 ms native dispatch/body boundaries, 2.028 ms ImGui setup and 1.387 ms packet
capture. Nested inclusive values overlap and must not be summed. cProfile likewise
ranks `_execute`, retained declaration and native wrapper orchestration first.
Reducing those costs while preserving state, input and layout is separate work.

### Backend and test evidence

The focused prototype/state/settings/startup/hotswap suite passes 314 tests.
Two additional native-settings tests reproduce the same backend-pointer failure
with automatic caching disabled: their pre-startup captured GLFW `swap_buffers`
receives a native Wayland `c_void_p` instead of `LP__GLFWwindow`. They exercise the
legacy settings surface, not retained `@gui`, and were not changed for this task.
Their failure logs are included rather than reporting the broader run as green.

A real native Wayland smoke run on the reserved agent desktop renders 10,000 rows
in each of two panels, including eight nested viewports. Wheel input scrolls each
panel independently, a floating portal increments to `Count: 1`, hide/show keeps
that state, and dragging its title moves the portal without disturbing row input.
Before/after screenshots are included. The smoke window is closed and the reserved
desktop released while preserving its pre-existing window.

Artifacts include the full default/calibrated/legacy matrices, raw timing samples,
frozen calibration observations, selection benchmark, plain/phase/cProfile output,
focused test log, isolated settings-failure controls and native Wayland screenshots.
