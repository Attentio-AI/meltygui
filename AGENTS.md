# MeltyGUI: contribution essentials

**Models expose stateful systems as Python values; plain views render them;
core_render injects reusable plumbing; Melty owns shared/global runtime state.**
These rules reduce boilerplate and maximize reuse of views and models.
Full design: [CONTRIBUTING.md](CONTRIBUTING.md). Existing code is not always the
intended pattern; see [known gaps](docs/ARCHITECTURE_DEBT.md).

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

## Verification

Preserve unrelated work/sessions; the editor refactor is deferred. Run focused
`.venv/bin/pytest` checks, broadening for shared changes. Verify UI on a reserved
agent desktop with separate session/save paths; follow `/home/lukas/AGENTS.md`.
Report GPU/backend coverage and remaining coupling. Test a noneditable wheel outside
the checkout before release. Import through `meltygui`, without sys.path hacks or
latent-descent dependencies. [Setup/checks](docs/DEVELOPMENT.md).
