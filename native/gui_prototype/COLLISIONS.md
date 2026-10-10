# Rust collision prototype

Development checkpoint: 2026-10-09. This extends the throwaway `@gui` path;
`render_func`, `ColumnLayout` and `RowLayout` keep their existing implementation.
The behavior reference remains [WINDOW_COLLISION_COLUMNS.md](../../docs/WINDOW_COLLISION_COLUMNS.md).

## Try it

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python examples/gui_collision_lab.py
```

The laboratory has three columns, nested rows, ordinary ImGui controls, an
internal window with its own columns, and a parent-relative nested window.
Checkboxes remove/recreate those windows through retained owner reconciliation.
It has its own app ID, `meltygui-collision-lab`.

```python
from meltygui import gui, os_window, columns, rows
import imgui

@gui
def content(label='Panel'):
    imgui.text(label)

@os_window(name='Columns', width=900, height=600)
def app():
    with columns(('left', 'right'), mins=(140, 180), padding=6) as layout:
        with layout.cell('left'):
            content(label='Left')
        with layout.cell('right'):
            content(label='Right')
```

`rows` takes the same options with axes exchanged. Keys can be a count or a
sequence of stable, unique keys. `sizes` supplies starting preferences;
`mins`/`maxes` are limits, and `fixed` supplies explicit fixed spans. Scalars
apply to every cell; sequences describe individual cells. A `None` maximum is
uncapped. Use distinct `key=` values for multiple same-axis layouts owned by
one view. A layout fills its owner's allocation or the active enclosing cell.
`layout.cell(key)` yields `(width, height)` and establishes allocation/clipping
for a cached child. Raw ImGui can also draw inside the cell.

Internal windows use `melty_window=True` and
`initial={'window_pos': (x, y), 'width': w, 'height': h}`. Their position is
relative to their actual declaration parent, including ordinary views between
window nodes. `min_width`, `max_width`, `min_height` and `max_height` constrain
the body. The prototype title strip is 28 pixels. `movable=False` and
`resizable=False` suppress direct window gestures; they are not docking APIs.

## What is implemented

- Rust owns persistent edge IDs, cell registrations, min/max propagation,
  duplicate-edge limit merging, fixed walls, feasibility checks and drag
  snapshots. Columns and rows use the same one-dimensional solver.
- Minimum compression pushes; capped expansion pulls. An uncapped span does
  not transmit a maximum pull. Connected layouts can push or pull their
  enclosing internal/native frames, subject to backend capabilities.
- Dividers use left drag. Right drag over a cell selects its local far edges;
  holding the second right click selects near edges. The other axis falls
  back to its enclosing window. Gesture targets stay captured until release.
- Unclaimed native background left drags pass to the existing host window-manager
  move gesture, as in core render. The retained graph observes the resulting OS
  positions and carries local child geometry; it does not issue competing moves.
  Internal windows, dividers and controls retain their own input ownership.
- Snapshots use total pointer displacement. Stationary frames do not keep
  moving edges, and out-and-back movement restores displaced geometry.
  Only the actively resized pair receives opposite-edge expansion at a wall.
- Internal windows retain parent-relative placement. Ordinary-view reflow
  carries cached windows without executing their bodies. Raising a parent
  raises its descendant windows in order, keeping them visible above it.
- Native containment orders individual window edges, retaining existing
  interleaving. Extra sibling contacts exist during native containment only;
  ordinary window movement does not install a general repulsion graph.
- Moving internal windows keeps the hard display-top boundary; resize uses
  all display edges. Where adjustment is available, contact can grow/move the
  native frame before reaching the display. A fixed native backend supplies
  immovable outer walls.
- Layout declarations survive a cached owner. Successful execution retires
  absent layouts; failed execution restores the previous geometry snapshot.
  Replacing outer edges rebuilds layouts that borrowed them. Minimum floors
  derive from current registrations instead of being permanently stamped.
- Layout-owned child allocation can change independently of the parent's
  Python body. Rust patches the framework child-image quad in retained packets,
  preserving command order, then composes the affected textures. GPU tests
  compare this result with a fresh parent capture.
- Raw ImGui geometry inside a changed cell recaptures its owner. An optional
  `@gui(freeze_resize=True)` child can retain its old pixels during a geometry
  gesture: top-left anchoring, original texel scale and current-cell clipping.
  Release redraws it once. Input, explicit invalidation and changed data wake
  frozen views; interactive contexts remain serviced.
- Absolute-position cache hits use a direct dictionary lookup. Geometry
  mutations clear the cache; reads do not walk parents on a cache hit.

## Ownership and native integration

The Rust edge graph is independent of rendering and platform APIs.
`gui_layout_prototype.py` owns declaration lifetimes, cell allocation, gesture
selection and the bridge to retained nodes. `gui_native_collision.py` connects
these edges to the native frame and display boundaries.

`surface_frame.py` chooses one solver: existing surfaces use `os_frame.solve`;
prototype surfaces use `NativeCollision`. The existing `os_frame.begin_frame`
and `flush` continue to own observation, expected/inflight geometry, capability
detection, compositor constraint learning and native requests. The new bridge
consumes pending native edge drags and commits one solution to that adapter.
It does not run the legacy solver over the prototype's edges.

Pointer displacement uses the **applied** native origin, separately from the
proposed allocation used for hit testing. A pending native move must not become
additional pointer travel on the next frame. Capability loss cancels the active
geometry gesture and its freeze state. Topology changes cancel retained pointer
gestures rather than applying their old snapshot to new edges.

## Development findings and limits

The work proceeded through the pure solver, fixed-bound retained layouts,
parent-independent packet placement, native-frame coupling, internal window
ancestry, input routing, and live backend checks. Several integration problems
were more important than the min/max arithmetic:

1. Retired children remained in bridge maps until after composition. Removing
   a window containing layouts exposed a stale-owner access. Retirement now
   runs immediately after the ownership graph commits.
2. Raising only a parent hid its nested windows behind its texture. Raising the
   window subtree preserves the required order.
3. A measured-height portal initially acquired a fixed allocation from the new
   geometry bridge. Capture now reconciles measured extents before placement.
4. Replacing a layout's outer edges left nested layouts attached to retired
   boundaries. Those dependent frames are now rebuilt with stable view IDs.
5. Shared native drag-token bookkeeping caused two-axis drags to restart one
   axis's baseline. Tokens and displacement totals are tracked per axis.
6. Using proposed native position for mouse displacement accumulates motion
   while an OS move is pending. An explicit late-acknowledgement test covers
   stationary motion before and after the request lands.
7. App-driving native background movement through the retained graph added the
   next-frame `os_frame.flush`/surface-request delay and depended on independently
   arriving local pointer and native-origin samples. This produced lag and jumps
   even in the minimal `imgui.text('hello')` app. Background movement now uses the
   host's existing WM gesture; nested movement and resize still use the graph.
   Routing tests attach native geometry and run `process_host_input` before host
   dispatch, since testing the input router alone missed the competing owner.
8. Rebasing an ordinary layout owner translated its layout edges but omitted
   bound child frame edges. Fixed padding constraints then linked old and new
   origins; repeated captures grew the table by the origin delta until the
   invalidation limit tripped. Translation now includes every frame in the
   owner's allocation chain exactly once. The real table demo is exercised with
   nested movement, resizing, native boundary pushes and stationary holds at
   multiple native origins, including an ordinary intermediate view.
9. Giving a plain child the same width/height as its parent does not declare a
   collision relationship. The table demo now places its native table in a
   zero-padding filling cell: its outer row/column edges connect through the
   layout to the OS frame, so outermost-cell resizing changes the OS window.

This is still a prototype, not production feature parity:

- Geometry policy, hit testing and declaration transactions remain Python.
  Transactions currently clone the edge graphs; the solver also scans graph
  registrations. Component-local scheduling, adjacency optimization and a
  large-layout performance benchmark remain future work.
- This API does not adapt the production tile tree, docking, split/join menus,
  saved divider layouts, tile toolbars, or descendant scrollbar overlays.
  Generic frozen-child replay is implemented; production tile overlay replay
  is a separate integration task.
- There is no intrinsic-width negotiation, general anchor-expression system,
  native child OS-window API, or per-monitor DPI transition support.
- Initial size preferences may be redistributed to satisfy limits. Impossible
  fixed allocations raise an explicit error; no minimum is silently weakened
  and no new overlap policy is invented. A recoverable user-facing diagnostic
  for this case is not implemented.
- Geometry rollback covers retained declarations. It does not roll back
  arbitrary application mutations made by a Python body before it raises.
- Borrowed native platform plumbing remains substantial. macOS cooperative
  native movement, X11 and hostile compositor timing have not received live
  coverage in this pass. Linux native Wayland and GLFW-on-Wayland have.

## Verification

The final selected suite passed **371 tests**. This includes the prototype,
GPU replay, existing column/row solver, OS-frame, fallback and historical
collision-regression tests. The separate live-surface harness exception below
is not included in that passing count.

`tests/test_gui_edges.py` includes 30 generated differential cases against the
existing Python edge solver, plus shared-limit, rollback, cycle and reversal
checks. `tests/test_gui_collision_layout.py` covers allocation, packet pixels,
nested axes/windows, ordinary-view reflow, native pressure, interleaved edges,
two-axis reversal, capability loss, late acknowledgements, fixed/max spans,
borrowed-edge replacement and frozen texture clipping/release.

Live checks on an isolated desktop exercised both Linux native Wayland and
GLFW-on-Wayland: dividers, nested right/double-right drags, native pressure with
out-and-back reversal, internal parent/child movement, widget clicks and
conditional retirement. Preview windows were closed after verification.

The legacy `test_native_collision_surface.py` harness has two failing assertions
in this environment: it observes a 544 x 514 body in an 800 x 800 surface.
An isolated control restored the original unconditional `os_frame.solve()`
dispatch and reproduced the same failure. These are recorded separately from
the passing prototype and legacy solver checks; they are not counted as passes.
