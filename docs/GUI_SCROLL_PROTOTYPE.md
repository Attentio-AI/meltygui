# Relative geometry and cached scrolling prototype

For the complete design history, persistence/identity decisions, known issues
and quick-start guide, see [the Rust GUI prototype overview](GUI_PROTOTYPE.md).

Run the interactive lab:

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python examples/gui_scroll_lab.py
```

The lab has two 10,000-row panels, one behind eight nested viewports. Wheel over
the rows, edit a checkbox, drag the floating detail window, and hide/show the nested panel. The body counters make
cache misses and actual widget input visible.

## Geometry ownership

`left`, `top`, `width`, `height`, `scroll_offset`, and `content_offset` live in
native draw-state slots. The retained graph references those slots. It does not
own another mutable rectangle, and there is no geometry save/restore copy or
`move_window` operation. Layout constraints write parameters through the same
state mechanism. Measured extents and spatial indexes are derived runtime data.

Use the same names in calls, defaults, initial placement, state edits and exclusions:

```python
nested_gui(value, left=5)             # Keep the other axis's flow/default placement.
nested_gui(value, left=5, top=12)     # Position relative to the parent content origin.
nested_gui(value, initial={"left": 5, "top": 12})  # Seed once.
draw_state.left = 40                 # Updates placement without rerendering the body.
```

Axes resolve independently. Explicit call arguments override authored state;
otherwise state edits and initial values win over decorator/signature defaults.
Without a coordinate default, that axis follows the normal layout cursor.
Rows/columns continue to own the allocation of their cells. Cached bodies draw
in their own local space; `left` and `top` place that output in the parent.
Saved `window_pos` tuples are translated on load; the old spelling is not a live
API alias. Use `no_save=("left", "top")` to exclude both coordinates, or exclude
one axis independently.

Positions are relative to the enclosing retained content space. `content_offset`
locates the viewport within that placement (for example, below a window title).
`scroll_offset` translates the children's content space, not their stored positions.

Use `cache.current` to obtain a retained node handle inside a cached view:

```python
cache.position(node)                       # Viewport origin in surface coordinates.
cache.position(node, relative_to=parent, content=True)
cache.graph.convert_point((x, y), source=node, target=other,
                          source_content=True, target_content=True)
```

These queries work mid-frame without executing view bodies, including for
declared children whose bodies have never rendered. Handles are local to the
retained graph; they are not the persistent `draw_state.unique` identifiers.
This prototype does not convert between separate native OS surfaces.

Cached origin reads use a geometry-root revision, without parent validation on a
cache hit. A changed transform invalidates that root's derived queries lazily.
Scrolling composition and hit testing use local coordinates instead of asking
for every descendant's absolute position.

## Viewports and cache behavior

`@gui(scroll=True, width=..., height=...)` creates a viewport over child `@gui`
views. Give children unscrolled content positions with `left` and `top`. Raw ImGui
drawing in the viewport body is fixed chrome; put scrolling content in child
views. Large monolithic draw lists do not become tiled or virtualized automatically.

Each container has a local uniform-grid visibility index. Wheel input selects the
innermost viewport under the pointer that can consume the movement, falling back
to an enclosing viewport at a boundary. Nested clipping also applies to hit
testing. Pure wheel gestures defer hover settling until the wheel stops, rather
than repeatedly executing widgets under a stationary pointer.

State changes enqueue work:

- Position changes update placement and composition, without executing the body.
- Scroll changes update viewport composition and visibility, without rewriting
  descendant geometry or invalidating descendant bodies.
- Size/content changes invalidate affected bodies. Unchanged primitive writes
  do not enqueue work.
- Newly exposed uncaptured children render on demand. Existing captured children
  replay. Optional ancestor rasters are recomposited bottom-up; cheap viewport
  packets expand into the nearest raster boundary with automatic selection.

Offscreen capture subtrees are evicted using a 256-tile / 32 MiB soft budget.
[Automatic raster selection](GUI_AUTO_CACHE.md) charges packet plus actual raster
bytes, while retaining the same state and context ownership.
Visible, focused, active and floating-window resources are protected. Eviction
releases textures and ImGui contexts, while declarations, draw-state objects and
injected state remain. The existing hard texture budget still applies.

The native host supplies the title bar's legacy mask owner explicitly. This
avoids a full state-registry scan every frame, which otherwise dominated the
20,000-row live demo despite the viewport caches doing almost no work.

Hide/show likewise recreates rendering resources around the same session state.
Save eligibility controls what survives reopening; it does not delete hidden
state objects during the running session. Geometry persists as ordinary fields,
subject to `no_save`.

Constraint-layout overlays use the same visible-owner queries. Their edge-solver
projection is rebased at interaction/capture boundaries, not by moving persistent
state on every scroll. Scrolled row dividers and nested-window dragging have
regression coverage. The existing constraint solver still uses whole-solver
transaction snapshots when a body executes; that cold/layout-edit cost has not
been redesigned here.

## Measurements

```sh
.venv/bin/python tests/benchmark_gui_scrolling.py \
  --frames 80 --counts 100,1000,10000 --depths 1,8,32
```

Recorded at the scrolling milestone, before the final `left`/`top` API cleanup.
Measured on an RTX 4090, using 600-by-400 viewports and 20-pixel rows. Each warm
sample alternates between two previously captured ranges. CPU time covers the
state write, scheduler and GL submission; GPU time comes from GL timer queries.
Only the benchmark waits for query results. These are scrolling-path timings,
not complete application frame times.

| Rows | Depth | Median CPU | Median GPU | Resident texture bytes |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 1 | 54 us | 10 us | 2,400,000 |
| 10,000 | 1 | 48 us | 10 us | 2,400,000 |
| 100 | 8 | 126 us | 57 us | 9,120,000 |
| 10,000 | 8 | 122 us | 59 us | 9,120,000 |
| 100 | 32 | 390 us | 223 us | 32,160,000 |
| 10,000 | 32 | 361 us | 226 us | 32,160,000 |

All measured warm scrolls executed zero view bodies. Candidate counts per sample
were 65, 79 and 127 at depths 1, 8 and 32, independent of row count. Texture
figures exclude the small shared scratch pool and font atlas.

Nesting still costs composition passes and viewport textures. Initial declaration
and CPU state storage still scale with total row count: mounting 10,000 rows took
roughly 0.4 seconds in this run. A virtualized declaration/provider API and tiled
rendering for large individual views remain separate work.

## Verification

`tests/test_gui_scroll_geometry.py` covers coordinate conversion, mid-frame edits,
unchanged writes, indexed visibility, nested wheel routing, hit clipping, GPU
pixels, capture eviction, state identity through hide/show, and restart restore.
The existing collision, measured-layout, widget, persistence and GPU suites are
also exercised. Fractional logical extents remain fractional; readback helpers
use the rounded-up texture allocation dimensions.

The scrolling milestone passed 263 focused tests; the subsequent positioning
cleanup passed 273. The six legacy render-function pickle tests pass in isolation; collecting
them with chrome tests exposes an existing `_LazyRenderFunc._fn` recursion,
also reproduced on the untouched baseline.

One faulthandler-instrumented run terminated with a segmentation fault while
core files were being edited live. Clean launches and the regression suites
passed; native-module/live-edit reload robustness is not established by this
prototype.
