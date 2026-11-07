# Accounting for the full-cache wrapper overhead

November 7, 2026. CPython 3.12, Linux GLFW/OpenGL, RTX 4090.

The full-cache wrapper's overhead is spread across input delivery, layout,
renderer setup, declaration reconciliation and retained bookkeeping. Two matched
cumulative runs remove **11.93 ms** before touching the remaining capture glue,
and **13.09 ms** after simplifying it. CPU submission falls from **16.77 ms to
3.68 ms**. The separately constructed minimal cache in those same runs takes
**3.24 ms**; raw drawing takes **0.282 ms**.

This is an attribution experiment, not a production optimization. Removed
features are intentionally unsupported in these variants. No library runtime,
installed native extension, input implementation or geometry cache was edited.

## Matched workload and safeguards

The baseline is the full-cache annotation from `GUI_CACHE_ANNOTATION_COMPARISON.md`,
not normal dependency-injecting `@gui`. All variants execute the same 341-view
tree with one rectangle per body and no artificial computation. Each measured
cached frame must execute 341 bodies with checksum 58,311, upload 341 packets,
and refresh one raster. Initial/final pixels match raw exactly. Context identities,
packet bytes, raster storage, logical references and scratch allocations remain
stable. Removing work does not replace drawing with an old cached screenshot.

Each scenario is mounted normally before the ablation is installed. The native
layout bridge is genuinely attached in the baseline. The cuts then deliberately
assume fixed input objects, topology, explicit dimensions and placements. The
fixed-topology declarations still perform native graph declare/begin/commit until
the separate graph cut. The final capture-only step borrows the existing GPU cache,
contexts and packet owners; the normal cache remains responsible for closing them.

Run order is randomized and frame-interleaved. Cumulative results below average
two runs with 24 warm and 200 measured frames per variant (`cumulative-2.json`
and `cumulative-3.json`). An earlier seven-step repeat is retained as supporting
evidence in `cumulative-1.json`. No timing probes are installed in measured runs.

## Cumulative removals: where the time went

| Removed at this step | Mean CPU ms remaining | Incremental ms saved |
| --- | ---: | ---: |
| Baseline: full cache wrapper | 16.769 | — |
| Host/per-view input handling | 13.373 | 3.396 |
| Layout transactions, binding and native surface updates | 11.263 | 2.110 |
| Automatic raster selection and calibration | 10.976 | 0.287 |
| Renderer setup, state synchronization, ImGui groups and measurement | 9.121 | 1.855 |
| Dynamic declaration/state/argument reconciliation | 6.317 | 2.804 |
| Lifetime accounting, source/dirty scans and retirement polling | 6.083 | 0.234 |
| Retained graph execution/results/measurement and composition scheduling | 4.837 | 1.246 |
| Remaining per-view execution glue and placement machinery | 3.678 | 1.159 |
| Separate minimal cache reference | 3.236 | Not another cumulative step |

The first seven cuts save 11.933 ms. The eighth brings the saving to 13.092 ms.
The remaining 0.442 ms above the minimal cache includes the still-present public
host/frame path and fixed-declaration dispatch; it has not been independently
attributed. These increments depend on removal order. They telescope within this
matched comparison; they are not intrinsic standalone subsystem costs.

The renderer cut preserves identity enter/leave so the already-mounted tree does
not acquire different IDs. It bypasses the rest of CacheRenderer via the fixed
explicit-body call. The declaration cut uses mounted input/argument/geometry
snapshots and stable keys; it cannot handle changing topology or arbitrary new
caller arguments. The graph cut replaces graph scheduling with the known fixed
order, but still performs all captures and GPU replay. The execution-glue cut
uses the minimal capture procedure on the same already-owned resources.

## Independent cuts: verification without relying on removal order

Two separate runs, 24 warm and 200 measured frames, remove each feature family
alone from the baseline. Baseline mean is 17.249 ms in this comparison:

| Feature family removed alone | Mean ms saved |
| --- | ---: |
| Input handling | 3.528 |
| Layout work | 2.436 |
| Automatic policy/calibration | 0.430 |
| Renderer setup/grouping/measurement | 2.450 |
| Dynamic declarations | 3.761 |
| Lifetime bookkeeping and scans | 0.486 |

These separate savings overlap and must not be added. Different scene sets and
workstation variation also shift absolute baselines. Conclusions use within-run
comparisons, not subtraction from earlier benchmark reports.

## Narrower checks identify repeated operations

Two additional independent-cut runs use 24 warm and 150 measured frames, with
baseline mean 16.429 ms:

| Specific operation removed alone | Mean ms saved |
| --- | ---: |
| `ImGuiInput.apply` for each captured view | **2.610** |
| Host input routing, retaining a valid frozen IO snapshot | 0.487 |
| `ImGuiInput.finish` | 0.097 |
| Layout transaction snapshot creation | **0.813** |
| Native geometry commit | **0.717** |
| Declaration-time `state.configure` only | 0.448 |

The input apply path writes delta time, font scaling, pointer position, mouse
buttons, modifiers, wheel and text-input state into every retained ImGui context.
The existing code already avoids a full 512-key copy for every unfocused view;
this result is the cost remaining after that optimization. Both detailed repeats
support the input-apply finding (2.657/2.563 ms saved).

State configuration itself explains only part of declaration overhead. The larger
declaration cut also avoids caller/default construction, identity/binding lookup,
invocation snapshots, change detection, aliases and repeated argument validation.
These semantics cannot simply be dropped in a real application.

An untimed, separately instrumented baseline frame confirms:

| Operation | Calls per frame |
| --- | ---: |
| Host IO processing | 1 |
| Per-view input apply / finish | 341 / 341 |
| Layout begin / commit | 341 / 341 |
| Native geometry commit | **342** |
| Public host dispatch / cached execution | 341 / 341 |
| Cache flush / compose / selector entry | 3 / 3 / 3 |
| Retirement polling | 345 |
| Capture residency accounting | 341 |

These counts are attribution evidence, not timings. In particular, the 342 native
commits occur despite unchanged fixed geometry. Layout snapshots fork both edge
graphs and copy transaction metadata for each view. These are concrete targets
for preserving behavior while reducing redundant work; the ablations themselves
are not proposed replacements.

## Validation, scope and reproduction

All long comparisons and smoke runs pass pixel, body, upload, raster and resource
assertions. The focused cache-annotation/public-API/cache-policy suite passes
18 tests after the experiments. CPU submission and GPU elapsed samples are stored
separately; waits are outside the CPU timer and the clocks are never added.
There are no per-frame image readbacks. CUDA, native Wayland presentation,
other platforms and package release artifacts were not tested.

Use a reserved agent desktop and isolated XDG config/state/cache directories:

```sh
.venv/bin/python tests/build_bare_gui_experiment.py --destination /tmp/cache-ablation-native
.venv/bin/python tests/benchmark_gui_cache_ablation.py \
  --native-path /tmp/cache-ablation-native/target/release/lib_gui_native.so \
  --mode cumulative --frames 200 --warm 24 --seed 2027 \
  --output /tmp/cache-cumulative.json
```

Repeat with another seed. Use `--mode single` for independent subsystem removals
and `--mode detail` for narrower operations. Code is in `gui_cache_ablation.py`;
all patches are instance-local or isolated generated methods. Raw samples, logs,
the separate call-count script and test results are in `/tmp/codex-cache-ablation/`.
