# Window collision investigation

Investigation date: 2026-09-16. Intended behavior follows [WINDOW_COLLISION_COLUMNS.md](WINDOW_COLLISION_COLUMNS.md). The initial diagnosis below is retained as historical evidence. The subsequently authorized fixes are now implemented and verified as described in the resolution section; the interaction contract is unchanged.

## Source and evidence boundary

HEAD at start and finish was `8928a65cfe1fee47578b78e0515a03d3c5a57a01`. The checkout was already substantially dirty, including `core_render.py`, `column_core.py`, and `os_frame.py`. A starting full tracked diff and geometry-specific diff are saved at `/tmp/melty-window-investigation/initial.diff` and `geometry.diff`; the HEAD is recorded there too. These temporary files are local evidence, not repository deliverables. The immediately preceding comment/documentation cleanup is distinct from the executable edits already present. This investigation does not establish who made the executable edits or why.

The initial investigation added this report and `tests/test_window_collision_investigation.py`. The authorized follow-up changed only `column_core.py`, `os_frame.py`, these focused regression tests and this report. The initially failing regressions now pass; no failures are xfailed or hidden.

## Initial reproduction: ordinary views interrupted native collision ancestry

A transparent ordinary view must not change the geometry of the same parent/child Melty window arrangement. The new test takes an existing native-containment shrink case and inserts zero, one, or two zero-offset ordinary views between its movable Melty parent and child. The views are placement ancestors, not collision frames.

Initial content coordinates are parent `(x=100, width=400)` and child `(local x=200, width=260)`; both minimum widths are 200. Native content width shrinks through 500, 400, and 300. At 500 and 400 all three variants agree. At 300:

| Ordinary ancestors | Parent `(x,width)` | Child `(local x,width)` | Child content x |
| --- | --- | --- | --- |
| 0 | `(60,200)` | `(60,200)` | 120 |
| 1 | `(60,200)` | **`(20,200)`** | **80** |
| 2 | `(60,200)` | **`(20,200)`** | **80** |

The extra child displacement is -40, exactly the parent's -40 displacement in the last step. It is counted once by the child write-back and again by parent-relative placement. The invariant here is equivalence after adding transparent placement ancestors, rather than treating every numerical detail of the old solver as a new requirement.

Concrete source cause in `meltygui/core/windowing/os_frame.py`:

- `_colliding_windows()` and `_root_of()` successfully discover the descendant through ordinary views.
- `solve()`'s `collision_root()` (around line 1153) climbs only while the **immediate** parent is in `window_ids`. An ordinary view ends the walk, incorrectly classifying the child as a separate collision root.
- The driving-wall loop (around line 1257) also stops as soon as the immediate parent is absent from `frames`, omitting the real Melty ancestor's dependency.
- Fold-back (around line 1317) subtracts parent-induced displacement only when the **immediate** parent is in `after_a`. With an ordinary view there, it writes the whole solved screen delta into `window_pos`; actual placement subsequently includes the moving Melty ancestor again.

There is already a `_frame_parent()` helper (around line 244) that skips ordinary placement parents, but these three solver sites do not use equivalent ancestry logic. `_abs_left()` in `state/new_core_model.py` explicitly adds the parent's absolute position, local offset, scrolling and anchors, confirming why a screen-space displacement cannot simply be applied as an unadjusted local displacement.

### What passes, and what that proves

The new reproduction also runs existing child-move, parent-move and parent-resize scenarios through zero or two ordinary ancestors. These six cases pass, including a live inherited-width ordinary view for the far-anchored resize case. Thus the demonstrated fault is not that every nested move or resize fails: it occurs when native containment displaces the movable Melty parent and the solver writes both parent and child geometry.

All 122 existing `test_os_frame.py` cases pass in the combined focused run. In particular, the 24 parametrized inline-panel cases already cover two ordinary ancestors, both axes, movement/resizing and fractional reflow anchors, but their enclosing app root is **frame-pinned**, so it is omitted from the collision-frame set. That is a different path from a movable Melty parent and does not exercise the missing fold-back subtraction reproduced here.

The minimal reproduction uses the existing fake-window/native-feed harness. It does not render ordinary views or prove full cache/reflow correctness, and does not establish all reported complex arrangements have this single cause. Stationary/reversal and mixed-anchor reflow after the reproduced native-parent displacement still need dedicated coverage during a fix.

### Recommended fix

Separate placement ancestry from collision-frame ancestry consistently. Build a containing-frame relationship by traversing ordinary views, and use it for grouping/extent membership and dependency traversal. Do not rewrite `parent_window` to erase the actual placement parent.

For write-back, subtract the displacement of the child's **actual placement origin**, exactly once. For transparent near-anchored views this equals the containing frame's near-edge displacement and resolves the reproduced -40 double count. Simply replacing every immediate parent with `_frame_parent()` is not sufficient for general correctness: an intermediate view can reflow, scroll, or use a centered/far anchor, and its origin need not track the same corner of the containing frame. Existing `_anchor_base()`, `pin_origin()` and the post-layout `rebase_pin()` mechanism provide related machinery, currently booked for frame-pinned roots. Reuse a coherent placement-origin measurement/snapshot scheme for movable-frame collision results, preserving drag baselines and hotswap state.

Regression acceptance should include the new transparent-chain invariant in both axes, multiple Melty levels separated by ordinary views, parent move and resize, centered/far anchors with reflow, then stationary frames and reversal after containment contact. It must preserve internal-parent overflow and must not introduce ordinary sibling repulsion. Driving edges remain an implementation detail.

## Initial reproduction: native chrome/body disagreement

Current source still explicitly reserves top chrome for a headerless surface body:

- `surface.py` around lines 405–410 computes `root_fill = (width, height - top, top)` from `titlebar.top_inset()`.
- `root_view_kwargs()` around lines 580–607 places a headerless pinned body at `(0, top)` with the reduced height. A root with its own header instead starts at zero and includes that chrome row.
- `titlebar.top_inset()` returns the 30px control-row height when enabled. This **body top gap** is distinct from `window_inset()`, the outer surface border accounting still used by `os_frame.flush()`.

Against this actual caller contract, the starting dirty diff removes three pieces of body-gap accounting: `_frame_pass()` no longer records `surface_inset` or subtracts it from body height; `gap_lists(rigid=True)` now constrains the native/body near gap to zero; `solve()` no longer includes the body's gap in the native minimum. This is not merely a stale-test wording disagreement: the caller still asks for a nonzero gap and the solver actively closes it.

Existing focused tests reproduce four failures, all in the nonzero-gap cases:

1. Near body-edge drag changes local y from 30 to 0.
2. Far body-edge drag loses 30px of expected native span at minimum.
3. Compositor near resize gives body height 640 when the surface is 640 and the requested body height is 610.
4. Interior divider push leaves native top at 300 instead of expected 270. Trace shows body `(y=30,height=690)` rewritten to `(y=0,height=720)` by the solve.

Zero-gap equivalents pass. This demonstrates a deterministic body/native coordinate mismatch in the model. Because the wrapper reapplies its body placement next frame, this can produce layout disagreement across the solve/render boundary; visible flicker/overlap was not directly observed in this headless investigation and should not be claimed as measured.

### Recommended fix

Preserve the headerless caller's actual nonzero content origin through rigid body/native coupling, body-size write-back and native minimum accounting. Prefer an explicit surface/body geometry value owned by the surface contract, rather than treating a temporarily solver-mutated `window_pos` as the authoritative permanent inset. Keep the zero-gap/header-integrated root path unchanged. Restoring the removed arithmetic is a plausible minimal candidate, but must be verified against the caller's current geometry and pending native rebases, not accepted solely because it was present at HEAD. No new chrome design or inset policy has been approved by this investigation.

## Initial diagnostic verification

Executed from the project environment:

```sh
.venv/bin/pytest -q tests/test_surface_body_resize.py tests/test_os_frame.py
# 126 passed, 4 failed in 0.66s

.venv/bin/pytest -q tests/test_window_collision_investigation.py --tb=short --show-capture=no
# 7 passed, 2 failed in 0.27s
```

Logs: `/tmp/melty-window-investigation/focused-tests.log` and `nested-tests.log`. No exhaustive suite was run. No production fixes, gesture changes, GUI actions, session edits, or user-window manipulation were performed. This is CPU/model-level evidence using the mocked native geometry feed. GLFW and native Wayland GPU/compositor behavior have not been verified, so equivalent live behavior is not established. A follow-up fix should verify the concrete failing geometries in an isolated reserved desktop instance on each supported backend and then run the focused regression set.


## Resolution after authorization

The user authorized fixes and confirmed that file-open contents were changing position and height incorrectly inside the native window. Implemented:

- `column_core._frame_pass()` retains the headerless body's near inset on its frame edge and subtracts that inset when writing back native-driven body height.
- `os_frame.gap_lists()` preserves the nonzero rigid near gap; native minimum size includes the gap and the body minimum. The existing surface/root caller remains the source of body placement. This preserves current chrome policy; it does not create a new one.
- `os_frame.solve()` uses `_frame_parent()` consistently for collision roots, dependency walls and local-coordinate fold-back, crossing ordinary placement ancestors without replacing their actual `parent_window` relationship.
- Existing measured-placement rebasing now also covers ordinary views inside movable Melty parents. The solver retains the child's screen-space displacement; the already-existing post-layout `rebase_pin()` hook subtracts the actual placement-origin change once. When phase B carries the family as a block, its displacement is carried into the recorded origin rather than cancelled. This handles intermediate reflow rather than assuming every placement origin follows the containing frame's near edge.

No new DrawState fields, changed gestures, sibling-repulsion rules, core_render edits, or commits. Existing dictionary-based runtime state and callback entry points are retained for hotswap; no restart workaround was introduced. A live production-edit hotswap cycle was not specifically measured.

### Regression results

The new reproduction now includes zero/one/two ordinary ancestors, reflow fractions 0/0.5/1 of parent width change, and three stationary frames at every native shrink step. Child move, parent move and parent resize retain separate coverage. All 15 cases pass. The fake-window harness invokes the same post-layout rebase hook as the real wrapper, with its existing recursive placement function supplying the measured origin.

Focused command:

```sh
.venv/bin/pytest -q tests/test_column_edge_solve.py tests/test_column_frame_fit.py tests/test_surface_body_resize.py tests/test_os_frame.py tests/test_window_collision_investigation.py tests/test_glfw_window_editable_root.py --tb=short --show-capture=no
# 161 passed in 1.11s
```

Log: `/tmp/melty-window-investigation/final-focused.log`. The coordinating agent additionally reports the complete library suite passed with **681 tests and 402 subtests** (two warnings), and seven Pro tile/navigation integration tests passed. Combined library log: `/tmp/window-tile-library.log`.

### Live native-Wayland file-selector check

Used reserved `desk-3`, seat `agent-desk-3`, owner `window-collision-fix`, with an isolated standalone `draw_file_selector` native surface. App id `melty-collision-file-check`; state was redirected to `/tmp/melty-window-investigation/state`. No user windows or sessions were touched.

The file-open UI rendered below chrome at y=30. A plain right-drag reduced the native content size from 820×620 to 740×520; the selector changed from 820×590 to 740×490 while retaining y=30. A later right-drag expanded it to 800×560; the selector became 800×530, still y=30. Screenshots and geometry logs agree; content remained below controls and filled the body to the native bottom. The desktop and test process were released afterwards.

Evidence:

- `/tmp/melty-window-investigation/file-before.png`
- `/tmp/melty-window-investigation/file-after.png`
- `/home/lukas/.local/state/hyprland-desktop-control/launch/20260916-184154-env.log`
- `/home/lukas/.cache/meltygui/resize-117908.log`

The live check used native Wayland, not GLFW. An attempted double-right tool sequence opened the inspector and did not establish top/left-drag behavior; it is not claimed as a successful gesture test. Nested fractional-reflow/native-containment coverage is model-level, not a rendered complex-view reproduction. Existing tests cover both axes in related pinned-root paths, but the newly added movable-parent fractional-reflow matrix is horizontal. Multi-level Melty families with mixed pin modes and live vertical reflow remain useful follow-up coverage, rather than being claimed fully verified here.

## Follow-up: right-drag and actual native child picker

The previous standalone selector check did not establish correct behavior for
MCE's child native picker. Actual plain/double-right gestures exposed transient
errors that disappeared in settled screenshots. Per-frame instrumentation of
`draw_file_selector` and `draw_fast_file_explorer` showed the body/descendants
jumping above the chrome while the native top-left edge moved.

Additional causes and corrections:

- A native pinned body's local origin must remain at its surface inset while
  native frame edges move. `_frame_pass` still rebases local layout edges and
  sizes, but does not also translate that body.
- `core_render` positions the drawing cursor after the edge solve, including
  explicitly positioned native bodies. Otherwise descendants capture offsets
  from the pre-solve cursor and shift until a later frame.
- Floating-window initial placement/rescue does not apply to pinned native
  bodies. Its bottom margin had lifted the picker by 20px on its first two draws.
- Measured movable-child origins are booked only when containment actually
  changes the containing frame. Booking every native request had canceled
  legitimate later parent right-drag movement.
- Absolute-position cache keys include current parent origin and selected
  parent anchor. A same-frame parent resize can otherwise leave descendants
  using geometry cached before the solve.

The actual MCE picker was checked on a reserved native-Wayland desktop with a
copy of the saved session and separate `XDG_STATE_HOME`. Both real right-drag
modes now retain explorer origin `(5,30)` during the gesture in a current-source
instance, including changing sizes. Evidence is in
`/tmp/native-picker-repro/fresh-probe.jsonl`; the earlier failing capture is
`/tmp/native-picker-repro/probe.jsonl`. These paths are local diagnostic
artifacts, not portable repository fixtures. GLFW live coverage remains pending.

### Actual nested right-drag follow-up and hotswap validation

The prior one-step modeled gesture tests missed a cross-frame problem. A real native-Wayland harness on reserved `desk-2` contained a movable Melty parent, two ordinary views, and a child Melty window anchored to the ordinary view's far edge. Plain right-drag resizing the parent repeatedly expanded the native container. In `/home/lukas/.cache/meltygui/resize-361605.log`, frames 37–49, the child's local x accumulated `0 → -150 → -300 → -600`: alternate intended parent resize increments were canceled.

Cause: the previous generalized `_book_pin_rebases()` booked movable-parent anchors whenever a native request was flushed or acknowledged, even when the containment solver itself did not move the parent. The later hand increment then looked like unwanted native reflow. The correction snapshots placement origins before solving but books movable-frame compensation **only when that solver actually changes the containing frame's geometry**. Deferred native-request booking remains scoped to frame-pinned roots. The new twenty-step resize regression fails before the fix (`child local x=160`, expected 200) and passes afterward.

A second real-render gap was exposed by the same gesture: absolute child coordinates lagged on alternating frames after the local-offset accumulation was fixed. `DrawState.abs_left/abs_top` accepted a same-frame cache entry without checking parent geometry. `solve()` could populate it before a parent right-drag, and later layout reads reused it. The cache now validates the actual parent origin and parent anchor offset through ordinary ancestors, using the existing cache fields and a bounded cycle guard. Six new real-DrawState cases reproduced the stale result across both axes and near/center/far anchors before the fix; all pass afterward. No DrawState fields were added.

Actual current-source gesture evidence, process 486481:

- Plain parent right-drag: width **470 → 1070**, child local position remains **(0,0)**. Its absolute x advances with every parent increment to **990**, with no alternating-frame lag. Native content expands to 1250px wide.
- True double-right parent drag, low-level timed press/release/second-held-press: parent top/left moves **(+120,+60)**, width becomes **950**, and the far-anchored child's x stays **990** while its y follows the parent. A stationary held interval was included.
- Trace: `/home/lukas/.cache/meltygui/resize-486481.log`. Harness: `/tmp/melty-right-drag/repro.py`. A later attempted child out-and-back gesture hit an inspector overlay and is not counted as reversal verification.

The standalone reproduction's external source edits were applied using the existing in-app evaluator and `mcp_hotswap.hotswap_file`, preserving the live process and window state. This exposed two hotswap defects that also prevented the coordinating agent's picker fix from reaching existing views:

1. Whole-file recompilation preserved live enum members but left module classification tuples containing throwaway members. A real `DrawState` hotswap changed correct geometry `(430,40)` to `(130,140)` because `Anchor.TOP_RIGHT` no longer belonged to `RIGHT_ANCHORS`. Surviving enum members are now included in canonical replacements, and immutable module tuple constants receive a narrow canonicalization pass. The real state-module hotswap now preserves geometry and anchor identity.
2. Patching the outer `render_func` did not patch already-created `wrapper`/`draw_inner_main` closures. Existing nested functions are now updated by matching their defining namespace, filename and qualified local name, retaining function identity and closure cells. This also repairs wrappers stranded multiple code generations behind. Changed closure layouts are explicitly rejected rather than silently keeping stale code. Rollback restores the nested code objects.

The hotswap regression retains an existing wrapper and mutable call counter through both a normal factory edit and a simulated older-generation orphaned wrapper. Focused geometry/hotswap/relocation checks: **22 passed**; focused window/geometry checks: **153 passed** before the final hotswap additions. Logs: `/tmp/melty-right-drag/hotswap-tests.log`, `focused-after.log`.

The coordinating agent subsequently verified the full fix **in the same already-running MCE process** after applying the hotswap updates: a real native child picker double-right resize from **960×880 to 1020×940**, with **51 sampled explorer frames all at origin (5,30)**. A subsequent plain-right resize also passed: **115 sampled frames across both gesture modes** all retained (5,30), with final picker size 1100×990 and explorer size 1100×925. Evidence cutoff `/tmp/native-picker-repro/swap-frame` and successful module-swap statuses in `/tmp/native-picker-repro/hotswap.txt`. This replaces the earlier limitation that live hotswap was unverified; no user-session restart was required.

Only reserved agent desktops and isolated state paths were used. The nested-right-drag reservation was released, cleaning its test processes while preserving the two windows that already occupied that desktop. Live coverage here is native Wayland; GLFW and all mixed-anchor/deep-family combinations are not claimed exhaustively verified.

Final validation: the complete library suite passed **695 tests and 402
subtests** (`/tmp/native-picker-repro/final-full-tests.log`). The focused native
body, containment, cached-geometry and hotswap run passed 163 tests. After
replacing the GC scan's `isinstance` check with exact function-type identity to
avoid invoking unrelated object proxies, the geometry/hotswap checks passed
again (`/tmp/native-picker-repro/final-hotswap-tests.log`). Test windows were
closed and reserved desktops released; user sessions and unrelated windows were
preserved.

## MCE workspace feedback reproduced and fixed

Recording the actual MCE inspector uncovered a remaining case after the previous
fixes. Its parent is the closable `workspace`, whose position, width and height
are supplied by the caller on every draw. The workspace is not a native pinned
root and is not freely resizable. An 80px inspector move grew the native surface
from 1589px to 2182px: each native growth resized the workspace, its right anchor
moved the inspector again, and that induced another outward push.

`os_frame` now recognizes native placement anchors through a chain of windows
whose caller supplies their complete frame. It measures the inspector's anchor
before a native request and compensates the subsequent layout displacement.
Freely placed/sized parents terminate that chain; intentional parent movement
and resizing still carry their children. Caller-controlled frames themselves
are not given persistent anchor compensation.

Sticky resize replay in `column_core` uses the same measured anchor, preserving
the compensation instead of restoring an obsolete relative position. On reverse
motion, a temporary containment shift of a caller-controlled ancestor is not
carried into the child's saved anchor: the caller replaces that ancestor's frame
on the next layout. This also fixes a 5px reversal error with two nested workspaces.

Eight new regression cases cover movement/resizing, both axes, and one/two
workspace levels, including reversal and idle settling. The earlier freely
resized-parent regressions remain passing. The focused collision suite passes
154 tests. No new DrawState fields or MCE-specific view workarounds were added.

Live native-Wayland verification used an isolated actual MCE process and applied
the changes by in-place hotswap. A 320px move consumed 160px of free space and grew
the native surface by exactly 160px; the inspector moved from x1502 to x1822.
A subsequent 120px right-drag grew its width from 520 to 640 while keeping x1822
fixed; another 80px resize retained the same near edge. Earlier and corrected
traces are under `/tmp/mce-feedback-fix/` and
`~/.cache/meltygui/resize-734195.log`. A fresh-instance video of move/resize and
reversal is `~/Videos/melty-drag-demos/mce-nested-parent-edge-fixed.mp4`.
Live GLFW coverage remains outstanding.

Final validation after the reversal fix: **703 tests and 402 subtests passed**
(two warnings), log `/tmp/mce-feedback-fix/final-tests.log`. The corrected
10.7-second MCE recording was uploaded to Hub Files as
`mce-nested-parent-edge-fixed.mp4`; it shows the same left-drag push/reversal and
right-drag resize/reversal. In the fresh video instance the inspector's final
x1139 plus width566 equals the native width1705, without cumulative growth.
The isolated editor was closed and its reserved desktop released, preserving
unrelated windows and the user's session.

## MCE nested workspace column right-drag routing

Reproduced in the actual editor with `editor.py` open in HEAD comparison: a right-drag inside either the file list or reference code pane enlarged the native frame while leaving the intended column boundary stationary. Before-fix recording: `mce-parent-columns-before.mp4` (Hub Files).

The workspace owns the nested column/row registries, but its caller supplies both width and height. `core_render` skipped its resize subscriptions on that basis; only the native pinned ancestor received the drag. Caller-sized Melty windows now participate in right-drag edge selection and queue the selected local edge through the existing solver. Their direct size-write fallback and corner handle remain disabled, since their caller owns their dimensions. Ordinary fixed-size views do not gain window resize subscriptions.

Verified by hotswapping the existing MCE instance: a 180px file-list drag moved its boundary by 180px with native width unchanged at 1777px. Reference/current column resizing, double-right left-boundary selection, reversal, and direct left-divider dragging also worked. The after recording `mce-parent-columns-fixed.mp4` shows the two plain-right gestures and reversal. `test_nested_layout_right_drag.py` covers both gesture directions, columns and rows, and caller-sized versus freely sized nested windows through an ordinary wrapper. Restoring the old subscription guard makes both caller-sized cases fail. Live verification used native Wayland; GLFW was not exercised live.

### Follow-up: caller-sized outer edges must resize the native frame

The preceding subscription fix introduced a confirmed regression: a workspace frame right-drag could compress its columns and move its opposite edge while leaving the native frame wider. Its caller then overwrote the local geometry. Interior divider routing was correct; outer frame ownership was not.

Caller-owned workspaces attached through caller-owned frames to a native root now forward frame-edge drags through the existing native-frame solver. Their local interior edges still use their own graph. The native minimum includes the workspace minimum and its layout margins, and the workspace retains its caller-supplied position and dimensions relative to the solved native span. Live hotswap verification compressed the 1600px native frame to 790px and shifted its near edge from x=1000 to x=610 under a 1200px inward drag, with no empty strip. Reversal and interior right-drag were recorded in `mce-parent-column-regression-fixed.mp4` (Hub Files). Four regression cases cover both axes and both frame edges through acknowledgement and reversal; they fail on the previous implementation. Full suite: 711 tests and 402 subtests passed.

The concurrently reported native left-drag failure remains under investigation. On the isolated agent seat, the application reaches `_begin_wm_move`, sends `xdg_toplevel.move` with a press serial, and returns success, but the window does not move. The desktop-control tooling documents compositor move grabs as unsupported for agent seats. This is not evidence that movement works on Lukas's main seat, nor sufficient evidence for an application-side fix.

### Native background left-drag: traced and fixed

The failure was reproduced in both the empty commit file column and the empty shortcuts area. Input dispatch captured `workspace..._window_hold`, so the drag never reached native chrome. This was a Melty routing failure independent of the agent-seat compositor limitation.

The workspace now explicitly declares `frame_pinned=True`, using the existing frame-ownership setting. A nested layout body with that setting and caller-owned geometry passes background drags to the native move handler. Fixed popovers retain their hold handler. Native frame-edge forwarding uses the same explicit distinction, rather than treating every fixed-size popover as native app background. No new DrawState fields were introduced.

After in-place hotswap, both empty-space gestures captured `enhanced_titlebar_strip` and called the native move request successfully. Agent input still cannot verify the compositor's physical move grab; the patched test window is left open on Lukas's desktop for his real-pointer check. `test_native_background_drag.py` covers workspace passthrough, popover absorption, and interactive-child priority. The pinned empty-background case fails under the old hold condition.

## 2026-09-17: integration failures after the tile-hosted editor

Lukas reported melty-admin resize failures, feedback loops and flicker, and
suspected the absolute-position cache work. The recorded runs and traces give
this chronology:

1. 09-16 21:21 (`e0fd6170`) and 22:35 (`504fd8e3`): the resize fixes and the
   geometry ownership refactor. Every melty-admin scenario passed on native and
   borrowed delivery between 22:28 and 22:42 against the workspace-based MCE.
2. 09-17 17:36 (`6be9992a`): `DrawState._cached_absolute_position` rewritten
   for speed. Its key still covers the parent origin and selected parent
   extent. A randomized check (400 chains, 30 mutations each, both axes,
   every anchor pair) found no difference between the cached and an
   uncached recomputation. The cache is not the cause.
3. 09-17 18:47: `melty_code_editor/editor.py` (uncommitted) dropped the
   `draw_editor_workspace` window and draws its tiles straight on the native
   root; each tile supplies `draw_code_editor`'s frame.
4. 09-17 18:48: the run failed. Every failure is the adapter looking up
   `draw_editor_workspace` (`view(..., 'workspace')`, `rim()`, the inspector
   ancestry check, the column scenarios); the gestures that ran were smooth.

Live probes of the current code (file-list, shortcuts and frame right-drags,
plain and double) showed no reversals or over-shoot in native bounds, column
edges or view rectangles. Two real defects did show, both present in the
09-16 recordings as well:

- RenderHost envelopes (`##code_cache_…`, 40 px, parked at x = -60) took part
  in the edge solver and native containment. Every frame the frame pass
  floored them at the axis minimum and containment pushed them to x = 0, the
  caller reset them, and the loop repeated: 156 consecutive solved frames in
  Lukas's live editor, and a 60 px sliver flickering down the left edge
  during native resizes (13 flips in `test_parent_resize_display_right`,
  every sample in the 15:16 column run). Fixed by keeping ``unmanaged``
  windows out of the frame pass and out of `_open()`; see the pipeline
  document's membership section and `tests/test_unmanaged_window_frames.py`.
- One missed Hyprland poll mid-drag flipped the feed unavailable, os_frame
  fell back to walls mode, and `_set_mode` discarded the gesture and every
  root's view of the OS edges (`geometry_mode` feed → walls at frame 552 of a
  probe run, `os_expected` reset). The feed now serves its last frame and
  retries for a short grace before dropping; `tests/test_geometry_feed_grace.py`.

The melty-admin adapter's workspace target now resolves the tile-hosted
`draw_code_editor` frame. The inspector opening on the first click of a
double-right gesture, and the inspector popup's on-display shift when the
native frame grows, are unchanged behaviors observed in both old and new runs
and are not addressed here.

