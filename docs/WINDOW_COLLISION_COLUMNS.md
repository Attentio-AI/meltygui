# Window, column and row collision rules

Canonical behavior specification, reconciled with Lukas on 2026-09-16.

This document governs intended behavior. Lukas's current clarifications take precedence over the [historical requirements review](COLUMN_WINDOW_REQUIREMENTS.md) and [implementation snapshot](COLUMN_WINDOW_IMPLEMENTATION.md). Historical column/row, contact-order and sticky-drag requirements are retained where they do not conflict with those clarifications. Source comments, solver structure and passing tests do not independently redefine these rules.

## Terms and scope

| Term | Meaning |
| --- | --- |
| Native / OS window | A real window supplied by the GLFW or Wayland backend, selected by the Toggle. “GLFW window” in older discussions usually means this outer native window, not a different collision policy. |
| Melty window | A window in Melty's internal window system. It is not an OS window. |
| Root Melty window | A Melty window without a parent Melty window. It can still live inside a native surface. Root does not mean native. |
| Nested Melty window | A Melty window whose position is relative to a parent. Its parent relationship participates in the geometry calculation. |
| Native child window | A native window with a native parent relationship. Its positioning semantics depend on the backend; do not assume it behaves like a nested Melty window. |
| Column / row | A layout cell bounded by edges. Columns constrain horizontal spans; rows constrain vertical spans. |
| Edge | A geometric boundary participating in a size constraint, physical contact or shared layout boundary. |
| Display boundary | The final screen boundary relevant to the interaction. It is distinct from the native window boundary, which may be movable. |
| Driving edge | An implementation concept used to derive a child's position from parent geometry. It is not a separate user-facing window type or a normative exception to the rules. |

“Parent” must identify which relationship is meant: containing OS window, parent Melty window, or enclosing layout cell. “Collision” must identify whether it means a layout constraint, contact with a containing boundary, or windows pushing one another under native containment.

## Core behavior

The user drags an edge to change geometry. A minimum span can push another edge; a maximum span can pull another edge. Connected constraints propagate the motion. Contact can propagate from a layout to its Melty frame, then to its native frame, and finally to a display boundary, where backend capabilities permit.

This does not enable general window-to-window collision. The native containment case below is the sole scope for windows pushing one another. Column/row constraints and a window meeting its containing or display boundary are distinct from making arbitrary windows repel one another.

## Gestures

| Gesture | Meaning |
| --- | --- |
| Left-click drag on a movable window | Move the window. |
| Right-click, hold and drag | Resize toward bottom/right. |
| Double-right-click, hold the second press and drag | Resize toward top/left. |
| Left-click drag on a column or row divider | Move that divider and propagate its layout constraints. |

Left-dragging any unclaimed empty space in a native app window moves that window; the header is only one valid area. Nested layout bodies belonging to that native frame must pass background movement through. Interactive controls keep their own gestures, and fixed floating popovers must not accidentally start native window movement.

Left+right-button corner switching is dead. Ctrl+right is also historical, not the current gesture. Do not restore either based on old conversations or comments. Double-right means the second press is held for dragging, not that a completed double-click starts an unrelated later drag.

A right-drag over a column must affect the appropriate column edge, rather than always resizing only the outer native window. Bottom/right selects the rightward boundary; top/left selects the leftward boundary. The vertical component acts on the applicable row boundary or enclosing window edge. Rows have the same behavior with axes exchanged. In a simple columns-only layout, horizontal movement changes the selected column boundary and vertical movement changes the enclosing window's top or bottom.

Nested rows and columns must select the applicable local boundaries. The precise hit-target algorithm is an implementation choice; it must not cause a native gesture to steal an intended inner layout gesture.

## Window-to-window collisions: native containment only

Windows collide with one another only when a parent OS window contains child Melty windows and must accommodate their layout. Its child Melty windows cannot extend beyond that containing OS window. When its available bounds press into the child layout, shift and compress the windows as constraints permit to keep that layout reasonable.

The native containment case includes resizing the native frame inward. It is not a general overlap-avoidance system:

- Ordinary dragging or resizing a Melty window must not cause unrelated Melty windows to push one another merely because their rectangles overlap.
- A nested Melty relationship alone does not enable sibling repulsion or introduce another window-to-window collision mode.
- Do not infer collisions between separate OS windows, or between arbitrary windows elsewhere on the display.
- An interaction inside a Melty window may push a containing native edge where supported. That contact does not by itself authorize ordinary sibling collision.

Within native containment, reason about individual edges, not indivisible rectangles. Preserve their order and the existing arrangement as far as constraints allow. Existing overlap is allowed: the goal is not to make all child rectangles disjoint.

For example, two windows may have vertical order `A.top < B.top < A.bottom < B.bottom`. Shrinking their OS container must account for all four edges. It must not skip B because it overlaps A, collapse the two bottom edges into one boundary, or replace this arrangement with a nonoverlapping stack.

A child can compress until its minimum size; contact then propagates to other edges and shifts the layout. Outside-facing edges meeting each other use contact without an invented outer margin. Historical requirements retain interior spacing/order for overlapping edges; exact spacing values are layout policy rather than a universal value specified here.

If available space cannot satisfy all minimum sizes, do not silently invent a new overlap policy or violate the display/size constraints. The historical request is to propagate the constraint to movable outer geometry, including opposite-edge motion when packed. The precise fallback when the backend cannot supply enough space remains unresolved.

## Nested windows and coordinate relationships

Nested Melty windows are part of Melty's own window system. Their positions are defined relative to their parents. Collision calculations must account for that relationship through the whole relevant chain, including ordinary nested views between window nodes.

Moving or resizing a parent may move a child even if no gesture directly targets the child. That resulting child geometry still participates in applicable boundary and native-containment calculations. A directly dragged child working correctly does not establish that a child carried by parent movement or resizing also works.

For an untranslated, unscaled parent-relative placement, the basic relationship is:

`child absolute position = parent absolute origin + child relative position`

Real calculations must include any applicable content origin or transforms. Compare edges in a common coordinate space, then apply changes back through the parent-relative relationship. Do not count parent movement twice, reuse a stale absolute position, or repeatedly add a displacement that the parent relationship already supplies.

A right-anchored child and a left-anchored child may depend on different parent edges. Handling those dependencies is an implementation detail. The requirement is correct resulting geometry, stable relative placement where unchanged by the interaction, and no feedback loops. “The driving edge is special” is not an additional product rule or permission to disable all child collisions.

If a native child window also uses parent-relative placement, its implementation must account for the relationship as well. Establish what that backend actually provides before assuming the same representation or algorithm.

**Regression coverage:** Lukas reported failures in complex arrangements with multiple nested views containing a nested Melty window. The reproduced ancestry/duplicate-parent-motion failure is now fixed and covered by focused tests, including ordinary-view reflow. The actual MCE inspector exposed an additional feedback path through its caller-sized workspace; native-growth anchor compensation and sticky reversal now cover that chain. See the [MCE reproduction and fix](WINDOW_COLLISION_INVESTIGATION.md#mce-workspace-feedback-reproduced-and-fixed). Full live complex-nesting coverage remains incomplete; see the [investigation and resolution](WINDOW_COLLISION_INVESTIGATION.md#resolution-after-authorization). These layouts remain supported requirements.

## Absolute-position caching is not negotiable

`DrawState.abs_left` and `abs_top` are read thousands of times per frame: every `on_action`, hover test, clip rect, pin and edge pass. **A cache hit must be O(1): one key comparison, no parent read, no walk, no per-level key construction.** Never remove, bypass or weaken this cache to fix a collision, resize or nesting bug. A collision fix that needs fresh geometry must invalidate the cache where the geometry changes, not re-derive positions on every read.

This rule exists because it was broken once. On 2026-09-16 a window-resize fix replaced the O(1) key check with a "validated" hit that reads `parent.abs_left` recursively and rebuilds a key at every nesting level, without saying so. Hits became O(nesting depth). Dragging a nested Melty window over the code editor fell from 120 fps to about 90, with roughly 18% of every drag frame spent in position reads; nothing in the collision tests noticed.

- The legitimate problem behind that change: a view's cached position goes stale when an ancestor moves or resizes *later in the same frame* (hand resize, native rebase, solver write). Solve it with invalidation keyed on real geometry changes, the way `_ancestor_scroll` is keyed on `Melty.scroll_version`. A write that does not change the value must not invalidate anything.
- Pinned views (`pin_to_clip`) and `pin_rect` targets follow the same rule. "Resolve live" is not a licence to walk the parent chain per read.
- Dragging a nested Melty window must leave the rest of the app frozen: cached views blit, and per-frame work outside the tile cache (position reads, edge passes, diagnostics such as `edge_motion_guard`, BVH sync) stays proportional to what moved, never to the number of draw states in the app.
- Any change touching `_cached_absolute_position`, `_abs_left` / `_abs_top`, `pin_rect` or their keys must be profiled on a nested-window drag in `melty_code_editor` (py-spy share of position reads, fps at a 1000 Hz pointer) before and after, and the numbers reported.

## Melty and native behavior should match where possible

The goal is for Melty and native windows to behave as closely as possible. The native backend is GLFW or Wayland according to the Toggle; the desired interaction model should not silently change with that choice.

Parity has real limits. Some OS environments do not let the app position native windows. A Melty window can extend beyond its parent Melty window; it is not necessarily clipped or contained by that internal parent. This differs from the containing OS-surface boundary described above. Do not turn the native-containment rule into universal containment within every Melty parent.

When a backend cannot move a native frame, honor the geometry/control capabilities it actually exposes. Do not pretend an OS move succeeded by changing internal coordinates alone. Backend restrictions should be documented as restrictions, not generalized into different Melty interaction rules.

Pinned app bodies, native roots, root Melty windows and nested Melty windows are implementation/integration categories with different coordinate relationships. They are not interchangeable meanings of “root.”

## Display boundaries: move versus resize

The hard display-top limit is a rule. Windows must not move above the top of the display.

Left-click movement may take windows partly outside the display at the other edges. Do not apply resize containment to left-click movement on all four sides. The later hard top limit narrows the older general permission to move partly offscreen.

Resizing collides with all display edges. The native window boundary is not the final boundary when it can itself be moved or resized; the display is.

Lukas reports this move/resize display behavior working as of this reconciliation. This is current user-reported status, not a new test result from this documentation pass.

### Contact order and opposite-edge motion

The retained resizing rule is that only the actively dragged view changes to moving its opposite edge when blocked by an immovable boundary. Other edges move through contact or their existing layout constraints, not because they inherit the active view's edge switch.

Example, resizing downward:

1. The active Melty bottom edge reaches the containing native bottom edge.
2. That contact pushes the native bottom outward, where supported.
3. The native bottom reaches the display bottom and cannot continue outward.
4. Continued resizing moves the active Melty top upward, enlarging the active view in the other direction.
5. The native top does not move merely because step 4 began. It moves only when contacted or required by an applicable constraint.
6. At the display top, further expansion is bounded. Do not keep sliding the resized window beyond the opposite display edge.

Apply the corresponding physical-contact rule in other directions. This is a resize rule; it does not erase the separate permissions for left-click movement.

## Column and row constraints

A column has a left edge L and right edge R, with width `R - L`. A row has a top edge T and bottom edge B, with height `B - T`. Each may have a minimum span and an optional maximum span. Sizes and limits are supplied by the layout; no universal numerical minimum or maximum is established here.

A starting width is not necessarily a permanent fixed width. Distinguish initial size, minimum, maximum and an explicitly fixed span. Likewise, drawing two nearby dividers does not automatically connect them: propagation follows the actual layout relationships.

### Minimum: compression pushes

When a cell reaches its minimum, continued motion of the dragged edge pushes the opposite edge so the cell does not compress further. That edge can then compress a neighbor until the neighbor reaches its minimum, continuing the cascade.

Example: a column spans `L=100, R=200`, with minimum width 60. Drag R left to 170: the width becomes 70 and L stays 100. Continue to R=150: retaining width 60 requires L to move to 90. That left edge can push the next constrained edge to its left. If the enclosing frame is reached, motion can propagate to the frame rather than simply freezing the original divider.

The same rule applies when dragging L right, and vertically when moving a row's top or bottom. Subject to actual barriers and available space, continued dragging expresses an intent to move the divider and push the constrained chain out of the way.

### Maximum: expansion pulls

When a cell reaches its maximum, continued expansion pulls its opposite edge with it. A maximum-constrained cell effectively carries its far edge once its slack has been consumed.

Example: a column spans `L=100, R=200`, with maximum width 120. Drag R right to 210: L remains 100. Continue to R=240: L must move to 120 to retain maximum width 120. Do not merely stop R at 220 while ignoring the remaining drag.

Several cells at their maxima can pull one another in a cascade. A cell below its maximum consumes available slack before passing the pull onward. An uncapped cell does not transmit a maximum-induced pull merely because a neighboring cell has a cap. A cell can still participate in minimum pushes.

Both sides of a divider matter: dragging it may compress cells on one side while enlarging cells on the other, causing a minimum push and a maximum pull in the same gesture.

### Rows, nesting and outer frames

Rows follow the same rules with axes exchanged. Do not implement weaker behavior for rows or omit their right-drag support.

A row inside a column and a column inside a row must propagate through their actual shared bounds. Geometry connected by the layout can constrain an enclosing frame. Unrelated layouts or windows do not become connected merely because they share a screen or DrawState implementation.

A divider reaching an enclosing frame can push it outward; a capped chain can pull an applicable frame inward. When that frame is coupled to a native frame, propagation must respect the coupling and backend capabilities. This is layout-to-frame propagation, not permission for arbitrary window-to-window collision.

At a true immovable boundary, respect the available span and connected constraints. Do not produce negative sizes or solve an impossible min/max combination by silently changing the user's limits. Exact precedence for contradictory constraints remains unspecified and should be resolved explicitly if encountered.

## Sticky reversal

The retained September requirement is sticky behavior across Melty windows, native windows, columns and rows: reversing within the same drag restores geometry displaced by that drag, including pushed or pulled edges and opposite-edge expansion.

Sticky does not mean permanently snapping to a contacted edge. Repeated frames at a stationary cursor must not continue growing or moving geometry. Reversing after reaching a display wall must account for the gesture's displacement rather than repeatedly accumulating corrections from already-clamped geometry.

The implementation may use snapshots, constraint solving or another mechanism. This document requires the behavior, not a particular algorithm. Exact behavior across external geometry changes, release/regrab, and simultaneous competing constraints is not fully specified by the recovered requirements.

## Native chrome inset: investigation and resolution

The independent source review observed working-tree edits removing fixed-inset accounting in `column_core.py` and `os_frame.py`, while existing surface-body tests expected the inset to remain. That was a source/test observation during concurrent edits, not proof of a regression or an intentional product change.

Lukas was not aware of an inset change and has not approved a new inset rule. Do not treat the earlier report as an accepted decision to remove or restore a particular implementation. Establish the relevant source revision, actual content/chrome geometry and observed behavior before deciding what to change.

After authorization to fix the confirmed file-open symptom, the implementation now preserves the existing headerless-body chrome offset and height. A live native-Wayland file selector retained its 30px gap through shrink and expansion. This restores the existing surface contract rather than introducing a new chrome policy. Follow-up checks of the actual MCE native child picker exposed transient right-drag errors missed by settled standalone screenshots. The body now keeps its surface inset, descendants capture the post-solve cursor, and both right-drag modes were verified during motion, including after an in-place hotswap. See [follow-up evidence and limits](WINDOW_COLLISION_INVESTIGATION.md#follow-up-right-drag-and-actual-native-child-picker); live GLFW verification remains outstanding.

## Acceptance scenarios

These scenarios define useful verification coverage; this documentation pass did not execute them.

| Scenario | Required observation |
| --- | --- |
| Plain and double-right drag | Bottom/right and top/left respectively; no left+right corner-switch requirement. |
| Column at minimum, dragged in either direction | Opposite edge pushes; neighboring minimum constraints cascade. |
| Column at maximum, dragged in either direction | Opposite edge pulls; slack is consumed before a pull propagates. |
| Same cases for rows | Equivalent vertical behavior. |
| Nested rows inside columns and columns inside rows | Applicable local edges move and shared constraints reach the correct enclosing frame. |
| Ordinary Melty drag across another Melty window | No arbitrary sibling repulsion. |
| Native frame shrinks around overlapping child Melty windows | Child edges shift/compress under containment, preserving their individual order and reasonable layout. |
| Melty child extends beyond its Melty parent | Do not impose universal internal-parent containment; still honor applicable native/display rules. |
| Direct child drag, parent move, and parent resize | Each produces correctly calculated child geometry; none substitutes for testing the others. |
| Multiple nested ordinary views containing a nested Melty window | Correct full coordinate chain, no double motion or feedback loop; retain regression coverage for the reproduced parent-motion bug. |
| Left move past display edges | Hard top limit; partial offscreen movement remains allowed at other edges. |
| Resize into each display edge | Boundary collision and correctly ordered active-view opposite-edge behavior. |
| Reverse after pushes, pulls and display contact | Affected geometry restores within the gesture without accumulating motion. |
| Same scenarios with GLFW and Wayland | Equivalent behavior where capabilities allow; explicit backend limits otherwise. |
| Body below native chrome | Investigate against actual geometry and the unresolved inset issue; do not invent a new rule from a dirty diff. |

## Evidence and precedence

1. Lukas's current reconciliation is authoritative for gestures, collision scope, parent-relative nesting, display behavior and native/Melty parity.
2. Earlier explicit column/row, sticky and contact-order requirements remain where compatible; their original messages are linked in [the historical review](COLUMN_WINDOW_REQUIREMENTS.md).
3. [The implementation review](COLUMN_WINDOW_IMPLEMENTATION.md) and [comparison](COLUMN_WINDOW_COMPARISON.md) are dated evidence records. They are not current product specifications and their gesture discrepancy is now resolved by this reconciliation.
4. Keep implementation observations and known failures separate from desired behavior. Update this specification when Lukas changes the rules, not merely when the code changes.
