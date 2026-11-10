# `@gui` full-redraw fast paths

November 10, 2026. CPython 3.12, Rust 1.77.2, meltygui-imgui 2.0.5 (2.0.4 for the first two passes), Linux
GLFW/OpenGL, Ryzen 9 7950X, RTX 4090. This pass changes the production
prototype (bridge and extension), unlike the earlier isolated experiments.

Goal: 500 small views on screen, all redrawing, inside a 120 fps frame
(8.33 ms). The worst case is every view invalidated every frame.

## Result

Every view invalidated every frame, one rectangle per body, native layout
observation and idle host input included. CPU submission, mean of 300 frames
after 30 warm frames, frame-interleaved with raw drawing
(`tests/benchmark_gui_redraw.py`, seed 1027; the "before" column is the same
script against commit 87f32626, 150 frames):

| Tree (depth:fanout) | Views | Raw ms | Before ms | After ms | After p95 ms | After us/view |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5:4 | 341 | 0.24 | 12.30 | **2.95** | 3.47 | 8.7 |
| 4:8 | 585 | 0.39 | 19.79 | **5.40** | 6.62 | 9.2 |
| 2:512 (one parent) | 513 | 0.35 | 18.86 | **4.90** | 6.51 | 9.6 |
| 6:4 | 1365 | 0.96 | 48.24 | **15.53** | 18.34 | 11.4 |

A second run (seed 2027) while another job loaded the machine measured 4.00,
6.47, 5.58 and 15.17 ms. Unchanged frames stay at 0.32 to 0.41 ms.

The matched four-variant benchmark used for the earlier baselines
(`benchmark_cache_annotation.py`, two seeds, isolated experiment build)
interleaves more scenes and runs colder:

| 341 views, mean CPU ms | 2026-11-10 baseline | After |
| --- | ---: | ---: |
| Raw drawing | 0.246 | 0.244 |
| Bare Rust forwarding | 0.358 | 0.354 |
| Cache-only annotation (no injection) | 10.831 | 3.390 |
| Full `@gui` | 11.437 | **3.520** |
| Minimal cache, full redraw | 2.012 | 2.025 |

Dependency injection now costs about 0.13 ms of that frame (0.6 ms before).
The official runner's immediate-mode wrapper went from 3.9 to 2.2 us per call,
and its retained scene from 0.155/0.201/0.274 ms to 0.112/0.144/0.215 ms
(leaf/divider/full).

**Reading the target:** 513 to 585 trivial views fit in 4.9 to 6.5 ms mean with
p95 between 6.5 and 8.0 ms. That is inside 8.33 ms with little headroom and no
body work; real bodies add their own time. Overhead is about 9 us per view,
versus 0.7 us raw and about 33 us before.

## Second pass: mounting and unchanged children

Thousands of small views (a word per view in a text editor) rarely all redraw.
They mount, and a parent re-executes over children that did not change. The
benchmark now also times a fresh mount (`--mounts`) and a root-only redraw
(`gui_parent` with `--hot`: the root executes, every other view is declared
again but stays cached).

Seed 1027, pinned to one core. "Before" is the first pass above, measured the
same day on a busier machine, so read the ratios rather than the last digit:

| Scenario | Tree | Before | After |
| --- | --- | ---: | ---: |
| Fresh mount, per view | 5:4 / 2:512 | 58 / 54 us | **21 / 20 us** |
| Root-only redraw | 5:4 (341 views, 4 declared) | 2.30 ms | **0.68 ms** |
| Root-only redraw | 2:512 (512 declared) | 3.71 ms | **1.64 ms** |
| Root-only redraw | 6:4 (1365 views, 4 declared) | not measured | 1.09 ms |
| Full redraw | 5:4 / 4:8 / 2:512 / 6:4 | 2.95 / 5.40 / 4.90 / 15.53 ms | **2.89 / 4.65 / 4.39 / 11.93 ms** |

The matched four-variant benchmark now measures 3.06 ms for full `@gui` on 341
views (3.52 after the first pass, 11.44 originally). An unchanged child costs
about 2.5 us to declare again, a fresh view about 20 us to mount, and a
recomposed frame about 0.35 ms on top of an unchanged one.

| Area | Change |
| --- | --- |
| Raster policy | A recomposed frame evaluates up to 192 reused views. Each evaluation rebuilt eight cost estimates; the terms are now memoized per snapshot by pixel count and payload size. This was about 2 ms of every frame in which anything changed. |
| Mount | Every identity compiled its own parameter plan (`inspect.signature` twice) and owned a native runtime. One runtime and plan now serve all identities of a definition; an identity adds only its state. A capture may start while an enclosing capture of the same function is on the stack. |
| Declaration | `Declarations.declare` resolves the identity, checks the remembered call and declares the view in one crossing. An unchanged child under an executing parent finishes there, including its layout advance and, under a non-scrolling parent, its image quad. Otherwise the bridge continues from the already declared node, or resolves fully when nothing remembered applies. |

## Third pass: native execution and a fused ImGui scope

A dirty view now executes in one native call, and its ImGui bookkeeping takes
two binding calls instead of about fifteen. This pass needs **meltygui-imgui
2.0.5** (`DrawCapture.begin_view`, `end_view`, `buffers`).

Seeds 1027 / 2027, pinned to one core, mean CPU ms:

| Scenario | Tree | Second pass | Third pass |
| --- | --- | ---: | ---: |
| Full redraw | 5:4 (341 views) | 2.89 | **2.26 / 2.23** |
| Full redraw | 4:8 (585 views) | 4.65 | **3.62 / 3.48** |
| Full redraw | 2:512 (513 views) | 4.39 | **3.30 / 3.21** |
| Full redraw | 6:4 (1365 views) | 11.93 | **10.81 / 9.89** |
| Root-only redraw | 5:4 / 2:512 | 0.68 / 1.64 | 0.68 / 1.67 |
| Fresh mount, per view | all trees | 20 to 24 us | 18 to 22 us |

Full redraw is about 6.0 to 6.6 us per view (33 originally, 0.7 raw). The
minimal-cache reference measures about 2.0 ms for 341 views in its own
seven-variant benchmark, where full `@gui` now measures about 3.0 ms.

| Area | Change |
| --- | --- |
| Binding | `DrawCapture.begin_view(w, h, x, y, label)` is `begin` + `push_id` + `begin_group`. `end_view(end_if_idle)` closes both, measures the content and reports whether the view owns the live widget; an idle view also ends its capture there and gets its draw buffer addresses back. `buffers()` returns those addresses for a capture that was kept open for input queries. |
| Execution | `Executor.run` replaces the Python `_execute`: begin, capture scope, body (the renderer is called directly, without argument marshalling), measurement, idle-input bookkeeping, packet copy, commit and residency in one call. The bridge's dictionaries stay authoritative; the executor holds references to them and skips writes that would store what is already there. It calls the bridge's hooks (`_layout_begin`, `_layout_end`, `_background`, `_commit_regions`, `_track_capture`, `_propagate`, `_abort_execution`, ...) only when layouts exist, a widget is live, a tint is set, a size changed, regions were staged or the body failed. |
| Declaration | A dirty child declared from its remembered call is handed to the executor inside the same `Declarations.declare` crossing, then concluded and placed there. The Python tail only runs for roots, new inputs, pending edits, layout cells, frozen views and path-like inputs. |

## What changed

Each item removes work a redraw repeated without needing its result.

| Area | Change |
| --- | --- |
| Declaration | A call with the same keyword arguments (identity or exact primitive, same order), the same flow position and unchanged state geometry reuses the previously resolved rectangle and arguments. The state remembers the geometry it was configured to (`remember_geometry` / `unchanged`), so values rewritten with the same result do not matter. Anything else takes the full resolution path, which is the previous code. |
| Native crossings | `declare_view`, `begin_execution`, `finish_execution`, `conclude`, `compose` and `raster_context` each replace several graph calls. The older single-purpose methods remain. |
| Renderer | `Renderer.execute` receives the frame context, current rectangle and explicit keys directly. A capture of a function without `**kwargs` layers framework values over the declaration's arguments instead of copying two dictionaries, and only materializes the framework values a parameter asks for. Field names are interned, core state fields have fixed slots, and ImGui entry points are resolved once per runtime. |
| Packets | `TextureCache.capture` reads the capture's finished ImGui draw lists in place (Dear ImGui 1.82 `ImDrawCmd` layout, checked against the index count) and reuses the previous packet buffers. No intermediate bytes objects or command wrappers. |
| Layout | Tables count their mutations (`_Tracked`); an unchanged layout shares one transaction snapshot between owners. Until the first row, column or window is ever declared, owners open their transaction lazily at the first layout mutation. The collision commit skips the platform adapter when the solver is unchanged. |
| Input | A view owning no live widget, pointer or focus skips reading the frame's IO. Idle frames skip enumerating the key table. |
| Hashing | Integer and field-name tables use a small multiplicative hasher instead of SipHash. |
| Bookkeeping | Per-node records are reached through one binding object; hit regions, events and residency are only touched when they change. |

## Limits and remaining cost

- The remaining 9 us per view is mostly Python orchestration in the
  declaration wrapper and `_execute` (about 3 us), the renderer with its five
  ImGui calls per view (about 2 us), capture begin/end and packet copy (about
  2 us), and per-frame composition plus raster policy (0.4 to 0.5 ms a frame).
- A call that passes a new object every frame (a fresh `initial={...}` dict, a
  new tuple or list) never matches its memo and takes the full path.
- With rows, columns or windows present, every view execution still runs
  `GuiGeometry.apply`, which walks all bound cells. That is unchanged and
  scales with the number of bindings times executed views.
- A parent with very many children still scans its child list per declaration
  and per commit.
- Wheels: only a local Linux CPython 3.12 wheel of meltygui-imgui 2.0.5 was
  built (kept in the binding checkout's `dist/local-2.0.5/`). The release
  wheels for other Pythons and Windows, and publishing, are not done.
- `capture` assumes this binding's draw command layout. A different ImGui
  build must fail its consistency check rather than be trusted.
- The remembered call keeps a view's last keyword arguments alive until it retires.
- Native objects are single-threaded. `close()` releases the declaration
  table and the shared runtimes on the owning thread; a cache that is never
  closed leaves them to a collection that may run elsewhere.

## Verification

- 332 focused tests pass (316 existing, 14 in `tests/test_gui_redraw_fast_paths.py`, and
  2 in `tests/test_gui_imgui_lifecycle.py` for the fused scope and a failing body),
  32 experiment-only skips. Two existing tests were adjusted to the new
  internals: the result-acknowledgement spy also counts `conclude`, and the
  hosted-transaction test asserts the snapshot after the first layout
  declaration instead of before it.
- The 27 cache-only, minimal-cache, public-API and policy tests passed against
  the isolated experiment build through the second pass. The cache-only
  annotation no longer applies after the third (see below).
- One of about 15 suite runs during the third pass printed "RetainedGraph is
  unsendable, but is being dropped on another thread"; ten further runs of
  this tree and of the original did not. Its cause was not found.
- `examples/gui_collision_lab.py` and `examples/gui_scroll_lab.py` were driven
  with the same clicks, drags and scrolls on this code and on a pristine build
  of 87f32626, after each pass. The resulting windows are pixel-identical
  apart from the frame-rate overlay. `examples/gui_imgui_widgets.py` was exercised by hand
  (button, checkbox, slider, text entry).
- All benchmark frames assert body counts and checksums; first and last frames
  assert exact pixels against raw drawing.
- Coverage is Linux Wayland/GLFW desktop OpenGL. CUDA, macOS, Windows, release
  wheels and a noneditable install were not exercised.

## Measurement harnesses

Still valid: `benchmark_gui_redraw.py`, `benchmark_gui_overhead.py`,
`benchmark_gui_scrolling.py`, `benchmark_cached_bare_gui.py`, `profile_gui_declaration_lines.py`,
`profile_gui_phase_taxonomy.py` in plain mode and `tools/run_rust_benchmarks.py`.
`build_bare_gui_experiment.py` was updated for the new renderer.

No longer applicable after the third pass: `benchmark_cache_annotation.py` and
`tests/cache_only_gui.py`, whose forwarding-only renderer the native executor
does not accept.

No longer applicable, because they patch source text that this work replaced:
`benchmark_gui_cache_ablation.py`, the coarse/phases/focused modes of
`profile_gui_phase_taxonomy.py`, `build_gui_declaration_prototype.py`,
`build_gui_records_prototype.py` and `benchmark_gui_full_cache.py`. Their
recorded results describe the previous code. `benchmark_gui_imgui_lifecycle.py`
already failed before this pass.

## Reproduction

On a reserved agent desktop with isolated XDG config/state/cache directories:

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python tests/benchmark_gui_redraw.py --trees 5:4,4:8,2:512,6:4 --hot --mounts 5 \
  --output /tmp/redraw.json
.venv/bin/python tests/benchmark_gui_redraw.py --profile --frames 100 --output /tmp/redraw-profile.json
```
