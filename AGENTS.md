# MeltyGUI: contribution essentials

**Models expose stateful systems as Python values; plain views render them;
core_render injects reusable plumbing; Melty owns shared/global runtime state.**
These rules reduce boilerplate and maximize reuse of views and models.
Full design: [CONTRIBUTING.md](CONTRIBUTING.md). Existing code is not always the
intended pattern; see [known gaps](docs/ARCHITECTURE_DEBT.md).

**All development is on `@gui`; `@render_func` is deprecated** (2026-11-25).
Assume every request, in every chat, is about `@gui` unless Lukas names
`@render_func` or the legacy code. Build new views, fixes and features in `@gui`;
do not add to or extend `@render_func` code unless he asks for that specifically.

**Rust `@gui`.** `@gui` is the Rust reimplementation of `@render_func`
(`@os_window`, `draw_editor`, `meltygui.rust.draw_any`); it sits beside the
legacy code the rules below were written for, and they apply to it as restated
in `GUI_REQUIREMENTS.md`. It was built in the worktree `~/Desktop/meltygui-rust-editor`
(branch `rust-editor-pathfinding`) and merged into `~/Desktop/meltygui`
(`melty-rust-prototype`) on 2026-11-13; both carry it.
Before writing a view or changing `@gui` code read, in `docs/`:
`GUI_VIEW_GUIDE.md` (how to build a view; its worked example is
`examples/gui_todo.py`), `GUI_REQUIREMENTS.md` (what Lukas decided, and these
rules restated for `@gui`), `GUI_PROTOTYPE.md` (how it works) and
`NATIVE_EDITOR.md` (the editor and `Source`). Most `@gui` views are public:
named `draw_<thing>` in `meltygui/view/common/<thing>_view.py`, published by running
`stubs.py` (it writes `VIEWS` in `meltygui/rust.py` and the IDE stubs) and used as
`meltygui.rust.draw_thing`.
**`@gui` views live in `meltygui/view/common/`**: the defaults (`@is_default_for` above
`@gui`, what `draw_any` draws a value with) and the common widgets (button, dropdown,
context menu, editor, tiles), as `common/<thing>_view.py`. Add new `@gui` views there, never
loose in `meltygui/view/`: that folder is the legacy `@render_func` views, which stay where
they are until `@gui` has replaced them and they are cleared out.
When Lukas decides something about `@gui` in chat, add a dated line to
`GUI_REQUIREMENTS.md` in the same turn.

## Ownership

- `view/<feature>_view.py`: local, plain render functions, never view classes or
  hidden global host/cache/focus lookups. Parent/sibling coordination over a
  repeatable local data shape is allowed, such as noncolliding dimension pickers.
  Walking to an app/root model couples the view to that app's schema. Judge reuse
  and caller boilerplate, not traversal syntax alone.
- `model/<feature>_model.py`: adapters and operations that hide stateful I/O behind
  dictionaries, primitives or renderable types. Use `RenderHost` only when its
  conversion/edit/persistence lifecycle is needed; a value proxy can own its resources.
- `state/<feature>_state.py`: explicit state and helpers. Inject per-view
  `DictConversion` instances; initialize fields in `__init__`.
- `core/`: shared injection, dispatch, conversion, caches, events, windowing and
  resource/lifecycle coordination. Complexity or GPU use alone does not make code core.
  Use its [responsibility folders](meltygui/core/README.md), such as `input/` for events.

Features need only relevant files; reuse across features. Demos belong in examples,
never library dependencies. Fix ownership, not just filenames; avoid catch-all modules.

## Render contract

Never use the Unicode middle dot (U+00B7) in UI text, including labels, buttons,
menus, and status messages. Separate details with spacing, parentheses, or clear wording.

- First parameter: typed `input_value`. Return accurate `(changed, value)`; mutate
  mutable input in place, preserving identity and specialized types. Layout/style
  belongs in signatures or decorators.
- Inject events; never poll imgui input inside cached render functions. Use
  automatic invalidation. No ad-hoc `DrawState` fields; framework-wide additions
  require Lukas's confirmation. Share model data, keep view-local state independent.
- Use Melty windows/controls, draw-list primitives and `draw_state` geometry.
  Tint renderers; keep local constants near the top. Shared settings use full
  `Toggles` chains, no namespace aliases or `getattr` fallbacks. Use `Tint` on dynamic
  backgrounds, literal f-string icons, descriptive names and how-to-change comments.
  These style rules also cover overlays.

## Runtime and changes

- On-demand features such as screenshots must do capture work only after an explicit request. Do not add per-frame allocations, copies/readbacks, GPU waits, drawable retention, polling, or permanently relax render-target optimizations for an occasional action. Schedule one-shot work on the owning render thread, wake it if idle, and release capture resources afterwards. Verify the normal rendering path is unchanged; do not claim performance impact is negligible without measurement.

- Consume background results before drawing; dispatch work afterwards. Call window
  lifecycles every frame with `open_requested`; both backends must behave alike.
- A window's starting size / position is `initial={"width":, "height":, "window_pos":}`
  (applied once; user geometry persists). `width=` / `height=` / `window_pos=` kwargs
  apply every frame and pin the window: never size a window with them.
- Make resource ownership/cleanup explicit. GL stays on the render thread;
  `ShaderRegistry` is not thread safe. Detect staleness via mtime, generations or
  identity, never whole-file hashes/comparisons.
- Subprocesses: full-path executable, `close_fds=False`, no `cwd`, `preexec_fn` or
  `start_new_session`; follow existing `posix_spawn` patterns, never fork the app.
- Hotswap every definition in place, preserving identities, callbacks and runtime
  state while applying source edits. Use canonical imports; update the library,
  `meltygui_pro` and `melty_code_editor` together, without legacy aliases or shims.
  Saved identifiers translate on load; source navigation follows actual imports.
- Report and fix framework gaps instead of hiding them with global lookups,
  disabled caches, per-frame invalidation or restart requirements.

For window, column/row, resize or collision changes, read the [reconciled behavior rules](docs/WINDOW_COLLISION_COLUMNS.md). Historical reviews and stale source comments do not override them.

For anything that draws or moves a tensor, read the [tensor rendering requirements](docs/TENSOR_RENDERING_REQUIREMENTS.md): a CUDA tensor is raymarched in place on its own GPU (80 GB at 120 fps), only the image crosses, and a failure is an error, never a copy.

## Designing a feature

Locality: each thing lives next to what it belongs to, at the smallest scope that
holds all of its users (code, state, UI, lifetime). A feature starts as one file in the app that
uses it and grows structure only when a second user appears. The full rule is in
[docs/APPS.md](docs/APPS.md#designing-a-feature); apply it to any new view, model or app work.

## Designing a feature

Locality: each thing lives next to what it belongs to, at the smallest scope that
holds all of its users (code, state, UI, lifetime). A feature starts as one file in the app that
uses it and grows structure only when a second user appears. The full rule is in
[docs/APPS.md](docs/APPS.md#designing-a-feature); apply it to any new view, model or app work.

## New `@gui` views and framework features

The global "New features" rules apply. In this project they mean:

- **Sizing contract.** Every view takes the standard `width` / `height` values from `GUI_VIEW_GUIDE.md` (`None`
  wraps, a number fixes, nothing or a very large number fills) plus `min_width` / `min_height`. Wrap means wrap,
  never fill, and nothing clips when wrapped.
- **Contexts to check:** a nested view, `melty_window=True`, `@os_window`, and a caller outside this checkout
  (`~/Desktop/cleanslate` or `~/Desktop/tile-view`) through `meltygui.rust.draw_thing`. Run `stubs.py` so the
  IDE sees it.
- **Degenerate values.** The view draws without crashing when called with no value (state injection), an empty
  value such as `()` or `{}`, and a view whose saved `draw_state` predates the change. Removing a parameter
  such as `melty_window=True` returns the view to its default.
- **Window input.** Left drag on empty space moves the window, right drag resizes, the context menu opens on
  right mouse up only when there was no drag, and hover does not flicker during a resize.
- **Idle and double render.** With no input the view does not invalidate, and no view body runs twice in a
  frame (the red double render mark must not show, also during window resize).
- **Numbers.** A framework change reports the `@gui` benchmark before and after, including the 512 view worst
  case (target under 8 ms) and input latency when frame timing is touched.
- **Nothing hidden.** No new parameter, mechanism, guide rule or framework check without Lukas asking for it.
  The report lists every public name it added. `GUI_VIEW_GUIDE.md` holds only what Lukas decided.
- **Show the usage.** The report includes a minimal snippet, caller side and view side, and a new feature gets
  a minimal demo in `~/Desktop/cleanslate`.

## Verification

Preserve unrelated work/sessions; the editor refactor is deferred. Run focused
`.venv/bin/pytest` checks, broadening for shared changes. Verify UI on a reserved
agent desktop with separate session/save paths; follow `/home/lukas/AGENTS.md`.
Report GPU/backend coverage and remaining coupling; never report git state ("not committed"). Test a noneditable wheel outside
the checkout before release. Import through `meltygui`, without sys.path hacks or
latent-descent dependencies. [Setup/checks](docs/DEVELOPMENT.md).

## Change summaries

After completing a bug fix or feature addition, include a concise Python
pseudocode summary of the changes in the final response. Use a fenced `python`
code block to show the key behavior or control flow, with brief comments explaining
what changed; for bug fixes, make the corrected behavior clear. Keep the summary
focused on the actual changes and identify it as pseudocode, not runnable code.
