# Cohesive Rust retained-declaration prototype

2026-11-07, CPython 3.12, Ryzen 9 7950X, NVIDIA RTX 4090. This is an isolated
prototype built from the production packet/bookkeeping implementation described
in [the preceding results](GUI_PACKET_BOOKKEEPING_RESULTS.md). **Production
declaration code and the installed extension are unchanged by this task.**

## Result

Moving the retained declaration orchestration into one Rust entry reduces
matched warmed CPU submission by **0.729 ms (4.4%) in the final validation-corrected
build**. Earlier randomized comparisons before the final error-path fixes
showed 0.9-1.0 ms (5.4-6.2%). This is useful but modest: Python
execution/layout/input hooks and extensive PyObject/dictionary work remain.
It does not make retained redraw comparable to raw drawing.

| 341 trivial views, native layout and idle host input | CPU median ms | p95 ms | MAD ms |
| --- | ---: | ---: | ---: |
| Production, final corrected comparison | 16.658 | 18.670 | 0.643 |
| Rust declaration, final corrected comparison | 15.928 | 18.331 | 0.615 |
| Production, earlier first paired run | 17.044 | 20.096 | 0.500 |
| Rust declaration, earlier first run | 16.119 | 18.927 | 0.513 |
| Production, earlier repeat | 16.946 | 19.613 | 0.661 |
| Rust declaration, earlier repeat | 15.902 | 18.005 | 0.665 |

Earlier main runs use 24 warm and 200 measured frames, randomized variant order,
frozen conservative cache costs and disabled calibration/probes. Pairing by
frame gives median savings of 0.919/0.976 ms; the candidate is faster in
90.5%/93.5% of frames. Raw medians are 0.253/0.254 ms. Hot replay stays around
0.38 ms. First-run GPU medians are both 29.696 us; repeat GPU medians are
27.136/27.648 us. CPU/GPU intervals are separate and are never added.
The final corrected comparison uses 24 warm and 150 measured frames. Its paired
median saving is 0.749 ms, with the candidate faster in 87.3% of frames. Raw
is 0.254 ms and hot production/candidate are 0.378/0.375 ms. GPU elapsed medians
are 20.992/23.552 us; no GPU improvement is claimed.

Fresh installed-production control was measured before implementation:
16.063 ms median, 18.149 ms p95 (200 measured frames). The larger live comparison
set changes absolute timings, so improvement claims use matched pairs rather
than that earlier control. Cold single mounts are 38.462/38.719 ms production/
candidate initially, and 38.697/37.402 ms on repeat; no cold-start improvement
is established from those single samples.

## What actually moved

`RetainedDeclaration` is one callable per compiled renderer plan, not a
collection of tiny exported functions. Its native entry consumes the fresh
kwargs dict owned by the thin public Python wrapper and performs:

* identity discriminator/handle and binding lookup;
* default/initial/caller preparation and geometry validation;
* state configuration through the existing native state conversion boundary;
* direct native graph declaration, previous-invocation comparison, aliases,
  parent relationships, dirty/cull decisions and result consumption;
* execute/flush decisions, final placement and return/state propagation.

The normal public `@gui` wrapper, callable identity and full NativeState values
are retained. Python still compiles signatures/annotations and cold bindings,
including the existing per-identity native renderer creation. Source-change
handling refreshes positional defaults in the same declaration plan. Scalar
plan references are snapshotted before callbacks, so reentrant refresh cannot
mix old and new defaults inside one call.

The native controller takes only short graph/identity borrows. They end before
Python allocation, binding, execution, layout, alias-string or flush callbacks.
The declaration uses immutable-self methods for refresh and recursion, rather
than holding a mutable declaration borrow through user code. GC traversal and
clear hooks cover the cache/function/wrapper cycle.

## What remains outside the migration

Python still owns `geometry.allocation`, `geometry.bind`, `_execute`, root
`flush`, optional child image placement, ImGui input/lifecycle/capture glue,
layout transactions and policy/composition. User render functions remain Python.
The existing native state-configure operation still crosses its Python binding;
it was not rewritten or given different conversion semantics.

The current dictionaries (`bindings`, `records`, `states`, `invocations`,
`caller_keys`, etc.) remain authoritative shared state for those hooks. Native
orchestration accesses PyObjects and constructs dictionaries; it is not a
fully Rust-owned retained runtime. It removes Python loops/branches and many
Python-to-native graph calls, not all Python-object overhead.

Source-level allocation accounting for a typical dirty declaration:

* Caller copy plus filtering becomes one filtered caller dict instead of two.
* Defaults and arguments dicts, and the independent invocation snapshot copy,
  remain. Keeping the latter avoids introducing shared mutable snapshots.
* The native execution callback currently constructs a keyword dict for
  `independent=False`, offsetting the saved caller dict in the ordinary redraw
  case. This is not claimed as a net per-view dict-allocation elimination.
* Interned plan keys, borrowed string views and direct caller-key tuple creation
  avoid prototype-only string copies and an intermediate key list.

These are source/call-count observations, not an allocator census. In the
matched scene, allocation and bind hooks each run 341 times, execution runs
341 times, and the full host frame performs three flush calls. The controller
still executes those required hooks; it does not bypass bodies, contexts or
input handling to manufacture a gain.

## Attribution, not performance projection

Separate coarse hook instrumentation measures declaration entry outside nested
Python hooks at **3.849 ms production versus 3.243 ms candidate**. The candidate
interval includes Rust control flow, PyO3/PyObject operations and existing
native state configuration; it is not pure Rust instruction time.

Those instrumented medians inflate to 22.565/22.803 ms, versus roughly 17/16 ms
uninstrumented. Therefore their percentages are not scaled onto production.
Candidate `_execute` self time is 3.882 ms in that instrumented run, with
additional input/lifecycle, capture, layout and composition below it. Allocation
and bind hook self times are about 0.052/0.072 ms; most residual cost is not
those two small callbacks but execution and shared Python-object state.

## Other controls

| Workload | Production CPU median ms | Rust declaration median ms |
| --- | ---: | ---: |
| Sixteen rectangles per view | 18.620 | 17.733 |
| 512 integer operations per body | 27.279 | 25.927 |
| Fully predeclared culled tree, five bodies | 0.803 | 0.738 |

These controls use 24 warm and 100 measured frames. Heavy checksum is
733,500,616,135; culled checksum is 15. Populated-layout one-leaf redraw at
1/16/64 layouts is 37.9/332.3/1341.4 us production and 38.5/344.9/1335.9 us
candidate: no material improvement is established when the declaration path
is not rerun and global layout work dominates.

**GPU caveat:** the sixteen-rectangle comparison has GPU elapsed medians
119.296 us production and 210.944 us candidate. Pixels and packet/resource
counts match, but the interval difference is unresolved. Scheduling/submission
gaps can affect these timers; that is not proof that the difference is harmless.
No GPU improvement or universal end-to-end renderer win is claimed.

## Correctness and limits

All measured frames assert body counts/checksums and exact raw pixels. Main
scenes retain 341 distinct IDs/contexts, one raster, 35,464 packet bytes and one
262,144-byte scratch target. Sixteen rectangles retain 567,424 packet bytes.
The culled scene retains 341 IDs and 257 resident contexts after normal eviction.
State and context object identities are checked before/after each timed run.
No alpha tolerance was silently introduced.

The final broad prototype suite passed 312 tests, with three experiment-only skips
and one explicit observer-proxy deselection. Focused coverage includes public
kwargs ownership, first aliases and precedence, reentrant declaration from a
layout hook, snapshot-consistent plan refresh, recursive body-code changes,
Python error propagation and cache/function cycle cleanup. Existing suites
cover persistence, pending returns, layout rollback, offscreen revisit, input/
focus, clipping/alpha, state identity and resource budgets.

The deselected test replaces the private native graph with a Python
`ObservedGraph` proxy. A direct Rust graph borrow cannot consume that proxy;
no Python fallback was added. A **separate test-only native-counter build**
passes all eight focused checks including equivalent lazy-ack assertions: unchanged input performs only the
consuming result read; changed input performs one acknowledgement read plus
the consuming read. That counter build is never used for performance.

Final parity checks also cover initial-validation short-circuit order, strict
native bool flags and Decimal height-bound ceiling/comparison behavior. These
are validation corrections, not another optimization stage; final verification
and a bounded post-correction timing are retained separately in the artifact.

This prototype needs a new native class and matching native graph types.
Already-loaded old extension objects cannot simply use the new entry. There is
no silent compatibility shim or live-native-upgrade implementation, and no
production adoption. Coverage is Linux GLFW/OpenGL, not full native Wayland
presentation, CUDA, other platforms, a noneditable wheel or native-module reload.

## Next architectural implication

The result supports a cohesive native declaration boundary, but not a naive
expectation that translating Python syntax alone removes most of the gap.
The next substantial step would make invocation/record state authoritative in
one place and expose narrow views to the remaining Python hooks, reducing
duplicate PyObject snapshots and preparation across declaration/execution.
That is a larger ownership change requiring the same persistence, return,
reentrancy and hotswap contracts; it is not implemented here.

## Reproduction

Graphics commands require a reserved agent desktop and isolated XDG paths.
No production renderer installation is performed by these tools.

```sh
.venv/bin/python tests/build_gui_declaration_prototype.py --destination /tmp/declaration-native
.venv/bin/python tests/compare_gui_declaration.py \
  --native-path /tmp/declaration-native/target/release/lib_gui_native.so \
  --native-layout --host-input --frames 200 --warm 24 --output /tmp/paired.json
.venv/bin/python tests/gui_declaration_prototype.py \
  --native-path /tmp/declaration-native/target/release/lib_gui_native.so --target pytest -- \
  -q tests/test_gui_*.py tests/test_retained_gui_*.py \
  -k 'not unchanged_caller_skips_result_acknowledgement_but_new_input_checks_it'
.venv/bin/python tests/build_gui_declaration_prototype.py --result-probe --destination /tmp/declaration-counter
.venv/bin/python tests/gui_declaration_prototype.py \
  --native-path /tmp/declaration-counter/target/release/lib_gui_native.so --target pytest -- \
  -q tests/test_gui_declaration_prototype.py
```

Artifacts under `/tmp/astra-gui-declaration/` contain samples, native source,
build/test logs and the explicit counter/performance build distinction.
