# Rust-owned retained bookkeeping experiment

2026-11-07. CPython 3.12, Ryzen 9 7950X, NVIDIA RTX 4090, Linux GLFW/OpenGL.
Built on the [isolated Rust declaration experiment](GUI_RUST_DECLARATION_PROTOTYPE.md).

**Decision: keep isolated.** The dictionary migration adds about 2% CPU improvement
on full redraw over the existing native declaration experiment. Hot replay is
slightly slower, and tail improvements are small. This does not justify promoting
the additional ownership machinery yet. A background-thread GC cleanup warning
also remains unresolved. Production and the installed extension
are unchanged. Public `@gui` signatures, injected values and draw_state behavior
are unchanged in the candidate.

## Matched results

Each main comparison uses 24 warm and 200 measured frames, with randomized
frame-interleaved order. All three implementations use the same native library,
341 trivial bodies, real native layout attached to a fixed surface adapter, idle
host input, frozen cache costs and disabled calibration/probes. CPU submission
and GPU intervals are measured separately. Raw medians are 0.274/0.260 ms.

| Implementation | CPU median run 1 / repeat, ms | p95 run 1 / repeat, ms | MAD run 1 / repeat, ms |
| --- | ---: | ---: | ---: |
| Current production | 18.121 / 17.178 | 21.912 / 19.514 | 0.621 / 0.468 |
| Prior native declaration | 17.288 / 16.337 | 20.236 / 18.940 | 0.646 / 0.433 |
| Rust-owned records | **16.928 / 16.014** | **20.228 / 18.708** | 0.636 / 0.460 |

The incremental record migration saves **0.360/0.324 ms (2.1%/2.0%)** by difference
of medians. Paired per-frame median savings are 0.359/0.314 ms; it wins 67%/74% of
frames. The total improvement over production is 6.6%/6.8%, but most of that gain
comes from the earlier native declaration port and must not be attributed to
replacing dictionaries.

Hot replay medians, prior native declaration versus records, are
0.464 -> 0.471 ms and 0.389 -> 0.402 ms. Main GPU medians match at 30.720 us in
run 1 and 29.696 us in the repeat. No GPU speedup is claimed.

An initial version rebuilt an ordered native snapshot every declaration. It was
slower than the prior native declaration: 16.582 versus 16.371 ms. Moving storage
to Rust alone did not help; avoiding unchanged snapshots was necessary.

Additional 100-frame controls:

| CPU median, ms | Production | Prior native declaration | Rust records |
| --- | ---: | ---: | ---: |
| Culled tree, five executed bodies | 0.825 | 0.780 | 0.781 |
| Sixteen rectangles per body | 19.525 | 18.597 | 18.251 |
| 512 integer operations per body | 27.181 | 26.206 | 25.968 |

The payload control has a GPU interval discrepancy: 219.648 us production versus
44.544/44.032 us for the two candidates. Pixels and packet counts match. This
remains an unresolved scheduling/submission measurement difference, not evidence
of a GPU improvement. No new populated-layout timing matrix was run; its behavior
is covered by the existing layout/collision tests. Global layout transaction
snapshots and scans were not changed by this migration.

## Storage and ownership

`RetainedRecords` owns one Rust `HashMap<u64, Record>`. Each row holds:

* One reusable execution tuple containing runtime, renderer, function, state,
  unique ID and ordered explicit caller keys.
* An immutable `Rc<Snapshot>` containing the latest caller input reference and
  an ordered Rust vector of argument key/value references.

This replaces the authoritative Python `records`, `states`, `uniques`,
`invocations` and `caller_keys` dictionaries. Read-only `Mapping` views for
records/states/uniques exist solely for inspection and existing tests; hot
framework code uses explicit native accessors. There is no duplicated backing
Python dictionary and no invocation/caller-key mapping adapter.

The declaration operates directly on the native table. Execution retrieves its
metadata in one operation. Layout/input use membership, argument and state
accessors. Host collision resize updates the last-caller snapshot explicitly;
this remains distinct from the graph's current execution arguments and pending
returned value. Retirement removes one table row. GC traversal visits the owned
Python references; table borrows end before callbacks and dropped references can
run Python destructors only after releasing the interior table borrow.

The immutable snapshot is reused only when input identity, ordered keys and
argument references are unchanged. Four internally synthesized numeric fields
(width, height, min_height, max_height) may match by exact primitive value because
they are reconstructed internally. Equal-but-distinct user argument or input
objects still replace the snapshot, preserving their latest identities. Caller
key order changes replace the execution tuple; identical keys reuse it.

Python user values and draw_state are retained by reference, not converted into
Rust data. The native graph still owns its execution kwargs dictionary, and
invocation materializes a fresh dict with current geometry for the Python body.
Caller/default/merged-argument preparation, binding caches, geometry transaction
snapshots, events, capture state and other runtime dictionaries remain. This is
not a complete dictionary-free runtime or a migration of arbitrary user data.

## Correctness and limitations

The broad candidate suite passes **319 tests**, with four experiment/test-probe
skips and one explicitly deselected graph-proxy test. **The final run also emits
one unraisable exception warning:** `RetainedRecords is unsendable, but is being
dropped on another thread`. An earlier broad run did not emit it. This is an
observed lifecycle failure in the new record table, not a passing correctness
guarantee. The failed destructor also leaves cleanup and retained-reference
release unverified. The current Rc/RefCell table requires owner-thread destruction;
a thread-safe Arc/lock implementation or explicit owner-thread cleanup would
need new validation and profiling. That fix is not included in this isolated
experiment. A separate instrumented
native build passes **all 16 focused tests**, including the equivalent lazy
result-read counts. That test build was never benchmarked.

Coverage includes existing persistence, mutable identity, pending returns,
geometry/scroll/cache/input, rollback, hotswap and resource tests. New checks cover
snapshot isolation from execution-argument mutation, caller-key order and tuple
reuse, equal-but-distinct caller identities, state replacement, and reentrant
Python destructors during replace/remove/clear.

A test-only native helper holds an Rc snapshot across a Python callback that
clears the graph/native runtime/table and calls `gc.collect()`. The test confirms
that only the snapshot owns the input, that it stays alive through the callback,
and that it releases after the helper returns. Existing cycle cleanup and
recursive source-change tests also pass. These are focused guarantees, not an
exhaustive proof for every possible user callback.

The graph-proxy test substitutes a Python `ObservedGraph` for the concrete native
graph. The direct native declaration cannot accept it; the separate native
counter verifies its lazy-ack contract without adding a fallback path.

Benchmarks assert exact raw pixels, body counts/checksums, stable state/context
identities and retained resource counts. Main scenes retain 341 IDs/contexts,
one raster and 35,464 packet bytes; the sixteen-rectangle scene has 567,424 bytes.
The culled scene retains 341 IDs and 257 resident contexts after normal eviction.
Coverage does not include full native Wayland presentation, CUDA, other operating
systems, a noneditable wheel or live replacement of a loaded native extension.

## Reproduction

Use a reserved agent desktop and isolated XDG config/state directories for GL
commands. The scripts compile outside the installed extension and alter Python
classes only within their process. The three-way comparison includes both the
production control and the earlier native declaration control.

```sh
.venv/bin/python tests/build_gui_records_prototype.py --destination /tmp/records-native
.venv/bin/python tests/compare_gui_records.py \
  --native-path /tmp/records-native/target/release/lib_gui_native.so \
  --native-layout --host-input --frames 200 --warm 24 --output /tmp/records.json
.venv/bin/python tests/gui_records_prototype.py \
  --native-path /tmp/records-native/target/release/lib_gui_native.so --target pytest -- \
  -q tests/test_gui_*.py tests/test_retained_gui_*.py \
  -k 'not unchanged_caller_skips_result_acknowledgement_but_new_input_checks_it'
.venv/bin/python tests/build_gui_records_prototype.py --result-probe --destination /tmp/records-probes
.venv/bin/python tests/gui_records_prototype.py \
  --native-path /tmp/records-probes/target/release/lib_gui_native.so --target pytest -- \
  -q tests/test_gui_records_prototype.py tests/test_gui_declaration_prototype.py
```

The next architectural question is whether compiled parameter slots can remove
repeated caller/default/argument preparation across declaration and execution.
Merely replacing efficient Python dictionaries with a second native table leaves
most Python-object work and layout/lifecycle cost intact; this experiment does
not implement that larger change.
