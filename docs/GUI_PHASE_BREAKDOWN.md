# Where retained GUI CPU time goes

Measured 2026-11-07 on CPython 3.12, Ryzen 9 7950X and NVIDIA RTX 4090.
Production Python and native renderer files are unchanged. This report follows
[the isolated dispatch experiments](GUI_DISPATCH_EXPERIMENTS.md).

That statement describes this profiling experiment. Subsequent production
packet/bookkeeping changes and their separate measurements are recorded in
[implementation results](GUI_PACKET_BOOKKEEPING_RESULTS.md); tables below
remain the original pre-implementation evidence.

## Main findings

The matched 341-view trivial-body scene takes **14.65-15.09 ms CPU** with the
actual native layout bridge attached. Its remaining cost is spread across
declaration/state plumbing, native dispatch, retained bookkeeping, layout,
ImGui lifecycle and packet capture. It is not predominantly rasterization.
GPU medians stay 17.408 us in the main baseline/probe controls; GPU elapsed
intervals include submission gaps and must not be added to CPU timings.

Three particularly concrete findings:

* Actual C++ `ImGui::NewFrame` costs about **0.944 ms across 341 calls** in
  this scene. C++ End plus Render add 0.080 ms. The larger 2.64 ms measured
  setup/finalization span also contains window setup, bindings, context/IO
  handling and surrounding Python work.
* Native layout cloning is only **0.074 ms**. The larger layout cost includes
  repeated commit/application work: 342 native-host commits cost about
  **0.583 ms**, even though the surface geometry stays unchanged.
* Packet byte arguments have a hidden conversion cost **before the Rust
  `add_packet` function starts**. Pinned PyO3 0.23.5 extracts `Vec<u8>` by
  iterating Python bytes one item at a time. Typed `PyBytes` plus owned Rust
  copies avoids that loop without exposing raw pointers or changing ownership.

The matched reference does not call the full host input router. Adding idle
`process_host_input` raises ordinary CPU time to **16.814 ms** (p95 17.833 ms).
The sparse lifecycle setup/finalization span rises from 2.64 to **4.48 ms**,
consistent with roughly 1.85 ms of additional per-view IO work. The original
15 ms reference and this fuller host-input case must not be conflated.

## What the category measurements mean

The primary scene has depth five, fanout four, 341 executed bodies and checksum
58,311. It retains 341 contexts/IDs, one raster, 35,464 packet bytes and one
262,144-byte scratch target. Conservative costs and calibration/probes are
frozen. `solve_native` runs with the actual NativeCollision implementation
and a fixed platform adapter. This is not a complete Wayland presentation
benchmark. Each run has 24 warm and 100 measured frames unless noted.

Sparse probes below time one family at a time and subtract complete nested
spans where needed. They perturb the workload less than the complete trace,
but they are **separate runs, not an additive pie chart of the 15 ms baseline**.
In particular, native dispatch includes its ImGui group enter/exit calls,
whereas lifecycle below covers the separate retained setup/finalization
blocks. Declaration includes its own graph/state calls; execution bookkeeping
includes graph work outside those declarations.

| Measured boundary | Observed ms/frame | Total CPU median in that probe run | Interpretation |
| --- | ---: | ---: | --- |
| Public call plus host binding lookup | 0.489 | 16.647 | Excludes complete retained declaration |
| Declaration, identity, state and arguments | 3.571 | 15.332 | Excludes complete execution and flush; includes native configure/declare |
| Native renderer dispatch outside Python body | 2.396 | 15.331 | Same-run Rust probes described below; includes group bookkeeping |
| Retained execution orchestration remainder | 4.385 | 19.674 | Excludes dispatch, layout, lifecycle and capture; more heavily perturbed and lower-confidence as a baseline estimate |
| Layout begin/commit/host application | 1.69-1.92 | 15.461-16.525 | Includes native-host commit; breakdown below |
| Retained ImGui setup/finalization | 2.636 | 18.023 | Input finish is excluded; IO apply early-returns in this reference |
| Packet extraction, copies and owner retention | 1.444 | 16.236 | Includes generated PyO3 argument extraction, unlike Rust-only timer |
| Policy, composition, eviction and presentation | 1.129 | 15.532 | About 0.326 ms selection and 0.786 ms composition |
| Python body callback self interval | 0.651 | 15.771 | Includes argument forwarding and traversal work, not just useful drawing |

These measurements support the ranking and identify concrete costs. They do
not justify multiplying fine-trace percentages by 15 ms. The useful drawing
reference remains the raw whole frame at roughly 0.24-0.27 ms; that includes
host submission, so it is an upper bound on raw body work, not a pure-body
timer. There is no defensible exact uninstrumented allocation of every
microsecond yet. Retained execution orchestration is the largest remaining
attribution uncertainty.

## Exclusive reconciliation and measurement perturbation

The complete trace uses a nested stack. Each parent excludes child spans and
their directly measured probe bookkeeping. Categories below are mutually
exclusive **within each instrumented run**. Graph boundaries are separate in
the fine trace; the coarse trace leaves them inside declaration/execution.
Fine tracing also separates individual ImGui calls from their calling native
dispatch boundary. The coarse trace does not, so those rows cannot be compared
as identical ownership boundaries.

| Exclusive category | Coarse mean ms | Fine mean ms |
| --- | ---: | ---: |
| Public/binding/native dispatch | 3.327 | 3.581 |
| Declaration remainder | 4.201 | 4.190 |
| Retained graph API boundaries | Included in surrounding scopes | 3.116 |
| Retained orchestration/bookkeeping | 5.085 | 5.981 |
| Layout transaction/application | 2.021 | 2.262 |
| ImGui setup/API/lifecycle | 2.790 | 4.864 |
| Packet capture/copy/ownership | 1.583 | 2.523 |
| Policy/composition/presentation | 1.121 | 1.488 |
| Body callback self | 0.722 | 0.709 |
| Host submission | 0.187 | 0.179 |
| Unattributed root interval | 0.004 | 0.004 |
| Sum of exclusive intervals | **21.040** | **28.896** |
| Directly measured probe bookkeeping | **1.522** | **6.554** |
| Instrumented root mean | **22.562** | **35.450** |

Reconciliation residual is below rounding precision. This is an accounting
identity, **not proof that the decomposition is unbiased**. Coarse/fine
medians are 22.110/35.081 ms versus ordinary controls near 14.9 ms. Coarse
tracing records 5,130 scopes/frame; fine tracing records about 24,670. Wrapper
calls, argument forwarding, context-manager machinery, Python allocations,
cache effects and instrumentation tails can inflate the observed categories
beyond the separately clocked probe bookkeeping. Those effects remain inside
the instrumented workload. They are not relabeled as production ABI overhead.

Ordinary controls before/after the final group are 14.975/14.852 ms (p95
18.482/18.850 ms, MAD 0.868/0.898 ms). All individual samples are retained.
The native-only probe run is 16.141 ms, approximately 7% above the earlier
15.088 ms ordinary control. CPU and GPU samples remain separate throughout.

## Native dispatch: inside and outside the Rust entry

The original isolated Rust probes give the following nonoverlapping sections
inside Renderer, excluding complete Python body intervals:

| Native section | Mean ms/frame |
| --- | ---: |
| Argument dictionary handling | 0.127 |
| Identity/runtime/state lookup | 0.137 |
| Geometry, state fields and event preparation | 0.563 |
| Parameter/default/injected-state resolution | 0.404 |
| Group enter and identity stack | 0.289 |
| Cleanup/group exit/state measurement | 0.284 |
| Return handling | 0.046 |

The enclosing outside-body native timer is 1.990 ms in that run; its difference
from the section sum includes probe/control-flow gaps. A separate simultaneous
Python/native run measures **2.396 ms** at the outside-body Python dispatch
boundary and **1.766 ms** within the Rust function. The roughly 0.630 ms
remainder includes wrapper forwarding, PyO3 ingress/egress, body-call boundary
differences and measurement effects. It is **not all demonstrated ABI cost**.
Native timings themselves include Python API calls made from Rust, not just
Rust instructions.

Outside Renderer, declaration's NativeState.configure timer is 0.482 ms and
identity.handle is 0.088 ms. Those are subcomponents of declaration, not extra
rows to add to it. No native identity.resolve calls occur on this retained
warm path: explicit retained identity is already supplied.

## ImGui work versus bindings and surrounding work

A small C++ helper links to the already loaded ImGui library, checks the header
version, and times its actual NewFrame/End/Render entry calls in the real scene.
The NewFrame bridge performs the same Python keepalive clear as the original
Cython binding. It does not introduce another ImGui implementation or share
view contexts. Ordinary total with these probes is 14.712 ms and pixels match.

| Actual in-scene C++ operation | Calls/frame | Mean ms/frame |
| --- | ---: | ---: |
| NewFrame | 341 | 0.944 |
| End | 341 | 0.014 |
| Render | 341 | 0.065 |

A sparse timer around the original Python NewFrame binding measures 1.185 ms.
The difference is a useful indication of binding/measurement overhead, not a
precise ABI subtraction across different runs. Begin-window setup, context
switches, IO wrappers, group handling and input bookkeeping still matter.

A separate texture-free lifecycle microbenchmark runs 341 dedicated contexts
through identical API sequences, alternating Python bindings and direct C++.
Final vertex/index buffers and draw commands match exactly:

| Body | Python sequence median ms | Direct C++ median ms |
| --- | ---: | ---: |
| No drawing | 0.743 | 0.403 |
| One rectangle | 0.793 | 0.406 |
| Sixteen rectangles | 1.380 | 0.485 |
| Empty-label real button | 0.800 | 0.421 |

These batched hot-context numbers are substantially below the interleaved
scene costs. They demonstrate working-set/batching sensitivity and possible
binding overhead; they do not replace the in-scene measurements or establish
that all of the difference can be removed while retaining application behavior.

## Layout is not mostly cloning

The no-app-layout host still owns native surface and screen constraints. The
sparse detailed layout run (15.461 ms total) separates:

| Layout operation | Calls/frame | Exclusive mean ms |
| --- | ---: | ---: |
| Begin transaction | 341 | 0.377 |
| NativeCollision.commit | 342 | 0.583 |
| Apply traversal outside native commit | 342 | 0.352 |
| Commit orchestration outside apply | 341 | 0.595 |
| Host solve outside its nested apply | 1 | 0.017 |

Native edge-graph fork time is only 0.074 ms across 682 calls in the separate
native run. NativeCollision.commit repeatedly reads four edge positions,
reconstructs pair/offset tuples, checks roots and calls the platform adapter.
The unchanged fixed surface has four edges and one constraint per axis.
Avoiding duplicate unchanged host commits is a stronger next hypothesis than
assuming cloning explains this category. Such a change needs generation-driven
invalidation and tests for native acknowledgements, moves, resize, root allocation,
dragging and rollback; this investigation does not skip those calls.

## Packet conversion before the Rust timer

`native/gui_prototype/src/texture.rs::add_packet` accepts two `Vec<u8>` values.
Pinned PyO3's `src/types/sequence.rs::extract_sequence` uses `seq.try_iter()` and
`item.extract::<T>()` for each element. Therefore its argument conversion runs
before the source probe placed at the beginning of add_packet. A small Rust
timer there cannot measure the entire packet boundary.

An isolated ownership-equivalent microbenchmark compares generic Vec extraction
with typed PyBytes plus `.as_bytes().to_vec()`. Both own two Rust vectors and
return checked lengths/content; a compiler black box retains the copy work.

| Combined byte payload per call | Generic Vec ingress us | Typed bytes with owned copies us |
| --- | ---: | ---: |
| 104 (one quad) | 0.756 | 0.063 |
| 1,664 (sixteen quads) | 9.363 | 0.086 |
| 65,536 | 377.305 | 0.946 |
| 524,288 | 3047.471 | 8.160 |

For 341 calls the first two differences correspond to roughly 0.24/3.16 ms
of **boundary microbenchmark work**, not verified whole-frame gains. In the
real scene, increasing one to sixteen rectangles grows packet capture from
1.444 to 4.833 ms and total CPU from about 14.9 to 18.945 ms. These repeated
rectangles may merge into one ImGui draw command; this is payload/primitive
scaling, not proof of sixteen times as many draw commands.

The fine trace counts 682 Python `ctypes.string_at` calls and 35,464 bytes of
Python byte copies per trivial frame, followed by two owned Rust vectors per
packet. It does not measure every allocator call or assert a newly allocated
Python integer for each byte (CPython can reuse small integers).

## Scaling and behavioral controls

| Ordinary workload | Bodies/frame | CPU median ms | p95 ms |
| --- | ---: | ---: | ---: |
| Single view | 1 | 0.208 | 0.234 |
| Flat 32-view tree | 32 | 1.406 | 1.632 |
| Depth-32 chain | 32 | 1.459 | 1.592 |
| 85-view tree | 85 | 3.396 | 4.046 |
| 341-view tree | 341 | about 14.9 | about 18.8 |
| 341 views, no drawing | 341 | 12.081 | 13.121 |
| 341 views, sixteen rectangles each | 341 | 18.945 | 24.801 |
| Fully predeclared culled tree | 5 | 0.514 | 0.637 |
| Hot replay | 0 | 0.125 | 0.179 |

Most cost scales with executed views, even when they draw nothing. At 32 views,
chain versus flat differs only about 0.053 ms in these runs: depth is not the
dominant explanation for this workload. This does not establish depth-independent
complexity for arbitrary layouts or deeper trees. Culling cuts execution work;
hot replay does not pay declaration/execution for every retained child.

Real empty-label buttons in the 256 leaves give 14.987 ms with 341 logical bodies;
actual C++ NewFrame/End/Render total about 0.964 ms. Widget-reference pixels are
compared against an independent uninstrumented **retained** widget scene.
Raw whole-window buttons were not pixel-equivalent because the retained widget
contexts differ; those attempted raw comparisons failed and were not used as
passing controls. No pixel tolerance was silently relaxed. A focused suite of
100 existing state, invalidation, hotswap, widget/focus, scrolling, geometry and
representation-transition tests passes under both fine probes and direct ImGui
probes. Idle timing does not establish the cost of text entry or active dragging.

## Next experiments, in order

1. Use typed PyBytes or a suitable typed buffer at packet ingress, retaining owned
   Rust copies. This targets a demonstrated linear conversion cost and avoids
   the raw-pointer exposure of the previous bridge prototype. Measure whole
   frames and preserve texture owners, validation and failure cleanup.
2. Coalesce unchanged native-host commit/application work using existing geometry
   generations, while preserving native acknowledgement and root-allocation
   rules. Do not infer eligibility from empty Python layout maps.
3. Reduce declaration dictionary/state/argument duplication with an explicit
   retained call contract. Keep default/caller/runtime-state precedence and
   source reconfiguration tests. Native configure is only part of declaration.
4. Investigate batching of lifecycle setup/IO calls only with explicit context,
   input/focus and keepalive ownership. Actual NewFrame is a real cost; removing
   private contexts is not established as a valid optimization.

The observed duration of a category is not its removable budget. These probes
identify where to test; they do not authorize discarding its semantics.

## Reproduction and artifacts

Run graphics commands on a reserved agent desktop with isolated XDG paths.
`tests/profile_gui_phase_taxonomy.py` supports ordinary, coarse, fine, sparse
and isolated native probes. The declaration probe is injected inside the
original closure so alias invalidation and `__wrapped__` retain their meaning.

```sh
.venv/bin/python tests/profile_gui_phase_taxonomy.py --output /tmp/plain.json
.venv/bin/python tests/profile_gui_phase_taxonomy.py --mode coarse --output /tmp/coarse.json
.venv/bin/python tests/profile_gui_phase_taxonomy.py --mode phases --output /tmp/fine.json
.venv/bin/python tests/profile_gui_phase_taxonomy.py --mode focused --focus layout-detail --output /tmp/layout.json
.venv/bin/python tests/profile_gui_phase_taxonomy.py --host-input --output /tmp/input.json
.venv/bin/python tests/build_gui_profile_native.py --destination /tmp/native-probes
.venv/bin/python tests/profile_gui_phase_taxonomy.py --mode native \
  --native-path /tmp/native-probes/target/release/lib_gui_native.so --output /tmp/native.json
.venv/bin/python tests/benchmark_gui_imgui_lifecycle.py --output /tmp/lifecycle.json
.venv/bin/python tests/profile_gui_phase_taxonomy.py --lifecycle-probes /tmp/lifecycle.so --output /tmp/core.json
.venv/bin/python tests/benchmark_gui_byte_boundary.py \
  --destination /tmp/byte-native --output /tmp/bytes.json
```

Artifacts under `/tmp/astra-gui-breakdown/` preserve samples, counters, probe
sources and tests. The shipped archive manifest distinguishes final reports
from exploratory/failed controls. Native builds remain outside the installed
extension. Coverage is Linux GLFW/OpenGL on NVIDIA, not CUDA, macOS, Windows,
native-module reload or a noneditable wheel. No production GPU waits,
readbacks, context removal or changed renderer semantics were introduced.
