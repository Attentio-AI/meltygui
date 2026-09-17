# Window geometry ownership

The behavior contract remains [WINDOW_COLLISION_COLUMNS.md](WINDOW_COLLISION_COLUMNS.md).
This describes the implementation after the geometry ownership refactor; it does
not introduce additional collision modes or gestures.

## Constraints and coordinates

`core/layout/edge_constraints.py` owns `EdgeGraph` and `solve_edge`. They operate
only on edges and cells: a minimum span pushes, a maximum span pulls, and walls
stop propagation. Shared edges are shared objects. The caller decides which
cells participate; equal or overlapping coordinates do not create contacts.

`column_core.py` builds local layout cells, manages layout registration and
translates delivered gestures into edge targets. `os_frame.py` adds native
containment contacts and display walls, and owns native request/acknowledgement
state. Native containment still uses individual frames followed by family
extents for residual motion; this remains separate from ordinary local drags.

Native and display edge objects always contain screen coordinates. `attach`
creates an `EdgeProjection` containing private copies in local coordinates.
Only a successful local solve calls `detach` to commit its native results.
Display walls are never written back. A failed solve cannot leave another
window looking at temporarily shifted native/display coordinates.

Gesture snapshots key native edges by their source identities, not by the
short-lived projection copies. Recreating a solve context must not reset a
gesture or lose reversal history. Surface-bound workspaces replay local edges
against the native origin, just like root bodies; replay must not move their
caller-owned position. The latter is covered by leftward column/row drags past
minimum spans and by sampled MCE shortcuts/file-list right drags.

## Surface-bound frames

`SurfaceBinding` describes the fixed near and far gaps between a surface body
and its native frame. Both a root below native chrome and an explicitly pinned
nested workspace use this same relationship. Fixed popovers do not acquire a
binding merely because their caller supplies a position and size.

The binding supplies fixed-gap cells to local layout solving and contributes
its content minimum plus gaps to the native minimum constraint. It is not an
additional free obstacle inside its own native frame. A workspace edge gesture
addresses the corresponding native degree of freedom; interior dividers still
address their local edges. A child's constraints never overwrite the ancestor's
declared minimum width or height.

Bindings are measured from the last layout geometry before solving. Measuring
the gaps against an already resized native frame would absorb the requested
resize into the margins. The same binding is used throughout that local solve.

## Resolve, commit, draw

Native containment reads window geometry for both axis solves before applying
any window position/size writes. It then commits the staged results. A vertical
solve cannot observe a half-applied horizontal parent resize.

Parent-relative placement through ordinary views may depend on layout that has
not run yet. Pending measured origins belong to the layout commit: they survive
both axis solves and additional native solves until the child's `rebase_pin`
consumes them, or the window closes. Native asynchronous motion continues to be
tracked separately by `apply_rebase` and the native acknowledgement queues.

For frame-edge handles, `DrawState.get_action` reads events already delivered
from the previous hit regions. The window solves both axes, then registers its
new edge hit regions using the final rectangle. The render wrapper places the
body cursor after the edge pass, so descendants draw from that same origin.

This remains an immediate-mode layout system: newly rendered layouts register
constraints for subsequent edge passes, and arbitrary caller-owned layout can
require deferred anchor measurement. The refactor does not claim that all view
layout is a single global pre-render pass.

## Verification

Regression coverage includes simultaneous native width/height changes through
ordinary parent views, failure isolation of projected edges, fixed-gap workspace
reversal, ancestor minimum ownership, committed frame hit regions, and the
in-place upgrade of the pre-refactor `Context` slot layout. Existing push/pull,
contact-order, chrome-inset, backend and hotswap tests continue to apply.

The comparison checkpoint is `9d72d155`. Recorded live scenarios are run by the
sibling `melty-admin` integration runner against MCE with isolated documents,
state directories and agent desktops. Native/borrowed are input delivery modes;
they must not be reported as Wayland/GLFW backend coverage.
