# Current column and window implementation

> Historical review, superseded as a behavior specification by [Window, column and row collision rules](WINDOW_COLLISION_COLUMNS.md). Lukas subsequently confirmed double-right-drag, narrowed window collision scope, and clarified nesting and backend parity. Preserve this document as dated evidence, not instructions to restore older behavior.

Independent cursory source review, 2026-09-16. No Claude conversations or historical requirements document were consulted. No implementation edits, UI session, or test execution were performed.

## Snapshot and limits

Checkout: `/home/lukas/Desktop/meltygui`. HEAD at start and finish: `8928a65cfe1fee47578b78e0515a03d3c5a57a01`. The working tree was changing concurrently, so this is not an atomic snapshot. Relevant dirty files: `meltygui/core/layout/column_core.py`, `meltygui/core/windowing/os_frame.py`, `meltygui/core/core_render.py`. `tile_manager_core.py` and `melty.py` were also dirty but not audited. The inspected input handler, collision helper, and layout view had no reported diff. Core-render diff hunk locations shifted during reading; references below identify symbols as well as approximate current line numbers.

“Source behavior” below is inferred from executable branches. “Test evidence” means existing assertions read, not tests observed passing. Comments that disagree with code are explicitly called out. Native backend integration and visual behavior remain unverified.

## Mental model

Two frame systems coexist. Each Melty window has a local edge graph for its frame and columns/rows. The native OS surface has a separate screen-coordinate graph used when its own edges change. Therefore ordinary Melty resize and OS resize do not have the same collision behavior: a Melty resize can pass through another window; an OS resize can compress and push a pile of windows while preserving existing overlap.

A layout cell is a pair of shared edge objects with a minimum and optional maximum span. A nested layout can adopt its enclosing cell boundaries, connecting constraints by identity. Nearby lines are not automatically connected: propagation follows registered cells. Each axis solves independently. Window position and size follow its near/far frame edges, and local edges rebase when the near edge moves.

Nested Melty windows use parent-relative positions. A special app root can be explicitly pinned to the OS frame; this is not a property of every root or every fixed-size child.

## Behavior and evidence

Paths in this table are relative to `meltygui/`.

| Case | Actual implementation | Source |
| --- | --- | --- |
| Initial sizes | Numeric entries seed pixel sizes; missing/None entries split remaining space. Floors can require overflow. Capped flex entries leave the pool and remaining space is redistributed. Default x/y floors are 60/40. Caps cannot undercut floors. | `core/layout/column_core.py:15`, `resolve_column_widths:162`, `_column_cap:153` |
| Push/pull | Compression to a floor pushes the opposite edge. Expansion to a cap pulls the opposite edge; uncapped cells never pull. Propagation follows connected cells, including shared nested boundaries. | `column_core.py`, `_solve_graph:449` |
| Walls | Wall edges cannot move; the dragged target is clamped through floor/cap chains. | `_solve_graph:483` |
| Nested layouts | Shared boundary identity connects parent/child layouts. Separate bands remain independent unless connected by cells. Keyed registries allow multiple layouts on one DrawState. | `ColumnLayout.__init__:1562`, `_REGISTRY:74`, `_edge_under_cursor:1436` |
| Divider drag | Resizable columns/rows subscribe to left-drag grab zones and queue edge motion. | `ColumnLayout.__init__:1727`, `RowLayout.__init__:2120` |
| Window frames | An expanded, measured window gets a once-per-frame edge pass. Foreign size changes enter that solve. Near-edge motion slides window_pos and rebases edges; far-edge motion sets size. | `window_edge_pass:1028`, `_frame_pass:1120`, normalization `1305`; `core/core_render.py:2877` |
| Right-drag | Plain right-drag selects bottom/right direction; double-right-drag selects top/left. Each axis latches the nearest eligible edge strictly ahead/behind the press, limited by the other-axis band. Frame edge is fallback; detached outer layout edges retarget to the frame. | `core_render.py:2286`, `2292`, actual `top_left_now:2365`; `column_core.py`, `_edge_under_cursor:1436` |
| Corner handle | Left-drag on the bottom-right handle resizes. Pinned roots do not receive this handle. Auto-resize and caller-passed dimensions gate resize paths. | `core_render.py:2275`, `2282` |
| Button chords | Left during held right is silent level state. Right during an active left drag is swallowed. Right after held, not-yet-dragging left cancels left capture and begins right behavior. Held left does not select the resize corner in current core_render. | `core/input/input_handler.py`, `feed_down:476`, `feed_up:550`; `core_render.py:2365` |
| Melty hand move | Left-drag sets relative window_pos from the baseline plus total movement. It can push the OS frame without applying sibling overlap avoidance. Right-resize and imgui ownership guard movement. | `core_render.py:2766`, `2792`; `os_frame.py`, `attach:730` |
| Top limit | Optional `Toggles.Melty.window_top_hard_limit` shifts a hand-moved window down after solving. A nested window adjusts its own relative offset, not its parent's. | `column_core.py:1297` |
| OS modes | `feed` uses the position feed, `x11` native position, `walls` immovable OS edges. Push behavior depends on toggle and geometry availability. Native minimum starts at 320×200 and can increase for pinned roots. | `core/windowing/os_frame.py:34`, `_enabled:131`, `mode:136`, `attach:730`, `solve:1202` |
| OS resize | Separate graph includes window screen-space frames with minimum floors and current-size caps, plus a position-ordered inter-edge chain. Global collision applies to native motion/gestures and pinned-root frame drags, not every Melty drag. | `os_frame.py`, `solve:1107`, graph construction `1209` |
| OS overlap handling | OS boundary and exterior-contact gaps are zero. Interior overlapping edges preserve spacing up to the axis floor, never greater than existing separation. This preserves order/overlap rather than making rectangles disjoint. | `_chain_floor:968` |
| Nested OS collisions | Open descendants of movable roots participate. Two-phase handling protects parent driving edges while descendants compress/slide, then adjusts ancestor extents when necessary. | `_colliding_windows:1031`, `_driver_of:1055`, `solve:1107` and phase branches |
| Native rebasing | Movable roots are closable parentless positioned windows. Nested children ride parents. Pinned roots ride the surface; applied origin and modeled origin can differ while native changes are pending. | `_root_windows:212`, `_movable_roots:239`, `_frame_pinned:248`, `applied_origin:709`, `apply_rebase:905` |
| Pin eligibility | Requires explicit `frame_pinned`, no parent, closable, `draggable=False`, and both dimensions passed. Fixed popovers do not qualify merely because they have dimensions. | `core_render.py:2106` |
| Gesture return | Native hand drags replay accumulated displacement from snapshots rather than repeatedly adding deltas to clamped geometry. Melty edge drags also have replay logic. | `os_frame.py`, `solve:1233`; `column_core.py`, `_replay_hand_drags:551` |

`view/layout_view.py:16` (`draw_columns`) and `:88` (`draw_rows`) adapt the layout classes to rendering: enter cells, render children, register child geometry, finish layout. They do not introduce an independent collision solver.

`core/input/collision.py:36` defines a separate `Collisions.resolve_collisions` rectangle-pushing helper. Repository search found only its definition, no active Python caller. `handle_collisions:24` does not actively resolve collisions. This helper is not evidence that ordinary Melty moves repel siblings.

## Existing test evidence, not executed

- `tests/test_os_frame.py:455`, `test_an_os_resize_pushes_windows_into_each_other_and_keeps_overlaps`, asserts compression and retained overlap/order under compositor resize.
- `tests/test_os_frame.py:482`, `test_melty_drags_still_pass_through_each_other`, resizes one frame edge through another window and asserts the second window is unchanged. Despite its broad name, this assertion exercises resize, not every move path.
- Nearby tests cover parent movement/resize making descendants push OS edges (`:657`, `:666`), nested top constraints (`:712`), and sticky return of nested tile edges (`:1687`).
- `tests/test_column_maxes.py` covers capped chains, wall clamping, uncapped breaks, cap-below-min, flex redistribution, and seeding. `tests/test_column_frame_fit.py:107` covers per-column minima; `tests/test_column_edge_solve.py` exercises the solver. These are model-level evidence, not backend verification.
- `tests/test_surface_body_resize.py:71` requires a body below 30 pixels of native chrome to retain its inset and settle. `test_interior_divider_pushes_surface_through_fixed_chrome_gap:99` compares against a graph preserving that fixed gap. These assertions are relevant to the dirty-source difference below.

## Working-tree differences from HEAD

1. **Native chrome inset removed.** `_frame_pass` no longer stamps `near['surface_inset']`; pinned-root size uses the entire OS span rather than subtracting the inset (`column_core.py`, near `1141` and `1250`). `os_frame.gap_lists:815` now uses rigid zero gaps at both ends; `solve:1202` no longer adds the pinned body's inset to the OS minimum. Existing surface-body tests still expect that gap. This is a concrete mismatch risk, not a demonstrated failure in this review.
2. **Parenting from plain view hosts added.** In `core_render.render_func`, near `1145`, if no explicit parent or Melty-window stack parent exists, an enclosing draw state can supply `parent_window = enclosing_view.parent_window or enclosing_view`. This changes positioned-child origins/clipping and potentially nested classification. `_frame_parent:257` skips ordinary view ancestors, but the entire interaction was not audited.
3. Other core-render differences concern multiple-renderer metadata, file metadata injection, and external mutation invalidation. No direct collision-rule change was inferred; indirect cache effects were not tested.

## Ambiguous terms and likely comparison risks

- **Window:** distinguish native surface, floating Melty frame, pinned app root, and nested popover, and name the driving gesture.
- **Root versus pinned:** pinning is explicit and conditional. The technical `_movable_roots` set can include pinned roots, which receive special handling.
- **Collision:** distinguish minimum push, maximum pull, global OS edge ordering, and rectangle overlap avoidance. Only the first three were found active in the inspected paths.
- **Fixed width:** a numeric column seed, a min/max constraint, and a caller-passed window dimension differ. A numeric column seed is not itself a permanent cap.
- **Under the cursor:** directional edge selection scoped by band, not general nearest-line distance. The target is latched for the gesture.
- **Left+right resize:** comments at `input_handler.feed_down:487`, core-render near `2296`, and `column_core.edge_under_cursor:1488` claim held-left corner switching. Executable `top_left_now:2365` uses double-right-drag only. A search found no `corner_drag_mode` or `is_down("left_mouse")` in core_render. Chord suppression remains implemented, but these comments describe a different corner-selection mechanism.
- **Screen position:** model and applied native origins differ during pending native acknowledgments. A single frame's geometry can mislead.

This is not an exhaustive registration, rendering, native backend, titlebar, or tile-manager audit. Focused column/OS/surface-body tests on a frozen checkout, followed by isolated gesture checks on the intended backend, would resolve the main remaining uncertainties.
