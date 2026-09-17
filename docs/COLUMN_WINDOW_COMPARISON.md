# Column and window rules: requirements versus implementation

> Historical review, superseded as a behavior specification by [Window, column and row collision rules](WINDOW_COLLISION_COLUMNS.md). Lukas subsequently confirmed double-right-drag, narrowed window collision scope, and clarified nesting and backend parity. Preserve this document as dated evidence, not instructions to restore older behavior.

Review date: 2026-09-16. Start here for the comparison, then use the two independent reports:

- [Recovered user requirements](COLUMN_WINDOW_REQUIREMENTS.md): an Astra agent read original user messages in Claude histories, without inspecting implementation. Includes dated corrections and 58 transcript links.
- [Current implementation](COLUMN_WINDOW_IMPLEMENTATION.md): a second Astra agent inspected source and selected test assertions, without reading the histories or requirements report.

This comparison was written after both reports. It does not change the intended rules or implement fixes. Source was changing concurrently around HEAD `8928a65c`; the code review is not an atomic snapshot. No tests or UI checks were run for this review. Existing tests are evidence of encoded expectations, not proof of current behavior.

## Memory-audit correction (2026-09-16)

A subsequent memory audit found that Claude's `project_wayland_resize_libdecor_cost.md` already recorded an August 25/26 user instruction replacing left+right with double-right-drag. The bounded history review missed this superseding instruction. Therefore the original gesture discrepancy below was a gap in recovered history, not evidence that the implementation changed against Lukas's wishes. Lukas has independently confirmed double-right-drag in the canonical specification. Stale source comments and conflicting active memory guidance have now been corrected; no gesture implementation was changed by the audit.

## Main finding

The historical requirements describe a consistent physical model with specific exceptions. Much of the implementation reflects that model. The recurring communication failures come from flattening those exceptions, using “window” or “collision” for several different things, retaining obsolete comments, and mistaking temporary rollbacks or one successful gesture for a complete result.

There is at least one concrete discrepancy between recovered intent and executable source: left+right-button corner switching versus double-right-drag. There is also an internal source/test discrepancy around native chrome insets. Other complicated behaviors require focused verification before calling them correct or broken.

## Comparison

Evidence labels such as E20 refer to the requirements report's source ledger. Implementation symbols and test references are in the independent code report.

| Topic | Recovered intent | Inspected implementation | Assessment |
| --- | --- | --- | --- |
| Minimum and maximum spans | Minimum contact pushes; a maximum makes one edge pull the other; both cascade. E34, E40. | Connected cells carry floors/caps; `_solve_graph` pushes and pulls. | Conceptual agreement; not runtime verification. |
| Rows versus columns | Same collision model, including right-drag. E38. | Axis-specific layouts use the same edge machinery. | Conceptual agreement. |
| Top-left resize gesture | Left+right replaces Ctrl+right and can switch seamlessly within a drag. E20. | `top_left_now` selects double-right-drag; held-left does not select the corner. Several comments still describe the chord. | Concrete discrepancy. No superseding user request for double-right-drag was recovered in the bounded history review. |
| Which windows collide | Ordinary Melty windows may overlap; native-window shrinking activates inter-window edge collisions. E58. | Local Melty graph and separate OS graph; existing tests explicitly distinguish pass-through resizing from OS pile compression. | Agreement that must not be simplified into “all windows collide.” |
| Overlap during OS shrink | Preserve the order of individual, potentially interleaved edges, including same-side edges. E64–E66. | Screen-space edge chain retains overlap/order; it does not make rectangles disjoint. | Conceptual agreement. Rectangle overlap avoidance would implement a different rule. |
| Nested windows | Normal collisions; only the relationship to the driving parent edge is special. Driver may be the parent's right edge. E70–E74. | Descendants participate; driver-aware phases handle compression and ancestor movement. | Broad agreement; exact phase behavior needs scenario verification. |
| Hitting the display boundary | Only the actively dragged view switches to its opposite edge. The outer opposite edge moves only after physical contact. E41, E52. | Walls, clamping and drag replay exist, but the cursory review did not prove the complete ordered sequence. | Verification gap, not a confirmed mismatch. |
| Sticky reversal | Reverse the mouse to restore displaced geometry, generally across windows, rows and columns. Renewed September 13, E102. | Native and Melty hand-drag replay mechanisms exist. | Mechanism present; full scope and combinations unverified. |
| Moving beyond the display | August 27 permits partial offscreen movement; August 28 adds a hard display-top clamp and extends it to nested windows. E57, A6, E78. | Optional top hard-limit path handles nested offsets separately. | Later correction explains the apparent historical contradiction. Enabling and backend behavior remain separate questions. |
| Native versus Melty roots | Standalone apps should preserve Studio interactions. A native child window is not the same thing as a nested Melty window. E97–E101, E111. | Explicit pinned-root eligibility and separate native-origin rebasing. | Terminology needs to preserve these distinctions; “root” alone is insufficient. |
| Content below native chrome | No precise inset formula established by the bounded historical review. | Dirty code removes fixed-inset accounting, while existing surface-body tests require it. | Internal source/test conflict risk. It is not evidence of a user-approved requirements change. |

## Where communication breaks down

1. **The nouns hide the object being changed.** “Window” may mean the native OS surface, a floating root Melty window, a nested Melty window, or a pinned app body. “Nested OS window” must not silently become “nested Melty window.”
2. **The initiating gesture changes the rule.** A divider drag, direct Melty resize, parent move carrying a child, and native outer resize can reach the same geometry through different paths. A rule inferred from one is not automatically valid for the others.
3. **An exception becomes a blanket exemption.** The historical corrections explicitly reject disabling all child collisions. Only the driving-edge dependency is exceptional. Likewise, only the actively dragged view switches opposite edges; a contacted outer window does not inherit that privilege automatically.
4. **Contact between edges is mistaken for avoiding rectangle overlap.** The user explicitly wants some overlapping windows to remain overlapping during OS compression. Ordering their individual edges is different from separating their rectangles.
5. **Comments preserve abandoned behavior.** The current gesture mismatch is a direct example. My earlier reply also repeated the held-left comment as current behavior; that claim was incorrect. Source inspection establishes double-right-drag as the current top-left selector.
6. **Chronology is flattened.** Temporary disabling of left/top collisions and sticky behavior does not erase the later requests to restore them. Conversely, an older offscreen-move request does not erase the later top-clamp correction.
7. **Verification covers only one direction or one layer.** Inner-to-outer pushes do not prove outer-to-inner shrinking. A directly dragged child does not prove a child carried by parent resize. Solver assertions do not prove native input delivery, chrome offsets, frame timing, or source hotswap.

## A precise format for future changes

Describe each intended change as a concrete scenario:

> Gesture and active edge → window kind and parent/driver relationship → first contact → propagated edges in order → final barrier → behavior on reversal and release.

For example: “Drag a Melty bottom edge downward. It contacts the OS bottom, which moves until the display bottom. Only then does the active Melty top move upward. The OS top stays still until contacted. Reversing within the drag restores affected geometry.” This wording preserves the distinction repeatedly corrected in E52.

For each scenario, record the coordinate space (cell-local, parent-relative, native-content or screen), backend/position-feed assumptions, and whether the statement is a user requirement, implementation choice, or unresolved detail. Pair a focused solver test with a native gesture check when the behavior crosses the OS boundary.

The first targeted follow-ups should be reconciling the gesture binding with the dated requirement and resolving the chrome-inset source/test disagreement. Those are separate from redesigning the edge solver. The two independent reports should remain evidence records rather than silently becoming a new, merged specification.
