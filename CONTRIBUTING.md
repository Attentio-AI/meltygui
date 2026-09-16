# Contributing to MeltyGUI

MeltyGUI makes reusable immediate-mode render functions easy to write. A feature
should expose its data in a useful shape and supply a view for that shape. The
framework should provide the repeated work of getting data, state and events into
that view. The rules serve two goals: eliminate boilerplate and maximize reuse of
views and models. Apply them by understanding those goals, rather than treating
structural independence as an end in itself.

This guide describes the intended design. Existing code includes partial
migrations; a function's current location is not proof that it follows the design.
See [known architecture gaps](docs/ARCHITECTURE_DEBT.md) for concrete examples,
[Development](docs/DEVELOPMENT.md) for setup and checks, and
[Building an app](docs/APPS.md) for the application API.

## The boundary to preserve

Render functions are local: their behavior comes from the value, parameters,
events and state supplied to them. They should be usable in another app or another
part of the same app without discovering a global host, registering a worker or
depending on a particular window.

Locality protects reuse. Walking ancestors to discover an app/root model makes
the intervening graph and that app's particular model schema part of the view's
implicit contract. That graph differs across views and applications; the farther
the view reaches, the fewer places its assumptions usually hold. Access through
`DrawState` does not remove that dependency.

Parent and sibling access can instead depend on a small, repeatable local data
shape. Three dimension pickers that coordinate noncolliding axis selections are
one such shape: the same relationship occurs in many places. Each picker is less
independent than a renderer that knows nothing about its neighbors, but the
coordinated behavior is still broadly reusable. A caller should be able to reuse
it by calling the picker a few times in a loop.

Judge which data shape the view assumes, where that shape can be reused, and how
much plumbing callers need. Local parent/sibling coordination is allowed; do not
move it into a model or core solely to satisfy a mechanical reading of the rules
and make callers rebuild that coordination themselves.

`Melty` owns shared runtime coordination. `core_render` and the rest of core inject
the dependencies and manage the lifecycle. Core exists to keep that plumbing
reusable and independent of the particular view. If a feature needs the same
plumbing that another feature already wrote, improve the shared mechanism.

Models make stateful things behave like Python values. A directory, process or
data source may need special handling underneath, but the view should work with a
dictionary, a primitive or a specific renderable type. Editing the value must keep
the connection to the underlying state.

## Files and ownership

| Location | Owns | Examples |
|---|---|---|
| `view/<feature>_view.py` | Plain render functions and presentation helpers for a feature | File rows, tensor controls, terminal cells |
| `model/<feature>_model.py` | Value adapters, domain operations, loading and applying edits | Filesystem reconciliation, tensor slicing, a dict-like metadata store |
| `state/<feature>_state.py` | Explicit state objects and their helpers | Selection, expanded rows, search text, per-view interaction state |
| `core/` | Injection, dispatch, shared runtime ownership and lifecycle | `core_render.py`, `melty.py`, modes, caches, events, conversion, window and backend integration |
| `examples/` | Sample data, demo composition and standalone sample apps | A demo volume and the windows that display it |

Use singular `view`, `model` and `state`. Views are functions grouped in feature
files, **not Python view classes**. A feature does not need all three files. A
model, state type or view can serve multiple features; give the shared concept a
clear name instead of copying it into each feature.

Core is grouped by responsibility: input/events in `core/input/`, render dispatch
and injection support in `core/rendering/`, conversion in `core/conversion/`, and
similarly named folders for the other runtime systems. Keep `core_render.py` and
`melty.py` as the main entry points; place new support modules in the appropriate
[core folder](meltygui/core/README.md). A feature's local views and models still
belong outside core.

Classify by responsibility, not by difficulty, imports or a `draw_` prefix:

- A stateful model still belongs in `model/`; having state does not make it a
  `state/` module. The model owns the value's meaning and how edits affect it.
- Per-view state is an instance supplied to the view. Shared registries, focus,
  scheduling and lifecycle belong in core, with runtime ownership in Melty.
- A dispatch wrapper such as `draw_any` is core plumbing even though it draws.
  A small presentation helper remains view code even without `@render_func`.
- GPU work is not automatically core. Tensor transformations are feature model
  work; presentation passes belong with rendering; shared context, resource and
  synchronization machinery belong in core. Review these separately.
- Examples and assets are legitimate directories. Reusable library code must not
  import a demo module to obtain its data types or drawing helpers.

Avoid new catch-all modules such as `utils`, `playground` or `new_*` for feature
implementations. Keep public exports available through `meltygui`; internal code
uses the canonical modules. Use explicit imports and descriptive names.

## Models that look like values

Preserve the ordinary behavior of the represented datatype. A dict-like adapter
should be usable through normal mapping reads and in-place edits. A typed
primitive should still support the corresponding comparisons, indexing or string
operations. Do not introduce a large feature controller that every view must
understand.

[TensorDim, TensorDims and Lut](meltygui/model/tensor_model.py) illustrate typed
primitives: they behave as `int`, `tuple` and `str`, while their distinct types
select suitable renderers. A renderer must return the specialized type after an
edit, or later frames will fall back to a generic primitive renderer.

[FileMetaProxy](meltygui/models/file_meta.py) illustrates the dict-like shape and
stable identity of a stateful store. Its current placement and shared-service
coupling are still migration work. These examples demonstrate specific aspects
of the pattern, not blanket approval of their surrounding modules.

Use [RenderHost](meltygui/core/conversion/render_host.py) when the value needs its conversion,
editing and persistence lifecycle. External state alone does not require a host.
For example, [TextureId](meltygui/model/texture_model.py) looks like an integer
texture ID while owning lazy, context-specific GPU allocations;
[LutPalette](meltygui/model/lut_model.py) exposes ordinary dictionary/list edits
and supplies those proxies. Neither needs a host running each frame.

Keep feature operations in the model and shared injection/invalidation in core.
A view may edit its supplied model through its public interface; it should not
implement file I/O, process management or synchronization protocols itself.

## Render-function contract

1. The first parameter is named `input_value` and is annotated with the type being
   rendered. Use `@render_func(is_default_for=...)` when this is a default renderer
   for that type, and give the render function a `tint=`.
2. Receive changing dependencies through the signature, with the local
   parent/sibling exception described above. Declare the events the function
   consumes, such as `left_mouse_clicked` or `left_mouse_drag`. Polling imgui input
   inside a cached render function bypasses event-driven invalidation.
3. Inject view-local state as a typed `DictConversion` instance and initialize its
   fields in `__init__`. See [file state](meltygui/state/file_state.py). Do not
   discover local state through `globals()` or add ad-hoc fields to `DrawState`.
   New framework-wide `DrawState` fields require Lukas's confirmation.
4. Keep layout and styling parameters in the decorator or signature, rather than
   serialized domain data. Declared parameters managed by the framework are
   different from arbitrary fields added to `DrawState` inside a view.
5. Mutate mutable input in place and return `(changed, input_value)`. For an
   immutable value, return `(changed, replacement)` and preserve its intended
   type. `changed` must accurately report edits: it drives invalidation and
   propagation to the backing model.
6. Use automatic invalidation. Do not repaint or invalidate everything every
   frame to hide a stale dependency. Scope hit regions with the existing event
   mechanisms; keep subscriptions working when the view serves a cached tile.
7. Importing a model type or a pure helper is normal. Looking up a host, focus
   owner, mutable cache or singleton service inside the function is a dependency
   that still needs to be supplied through the framework.

If the framework cannot inject what the view needs, or cannot invalidate or
hotswap correctly, report and fix that framework gap. Do not hide it with a global
lookup, blanket cache disable, per-frame invalidation or a restart requirement.

### Drawing and style

Use Melty windowing and existing controls such as `flat_button` and
`draw_dropdown`. Draw custom controls with draw-list primitives, using
`draw_state` geometry and content bounds. The
[draw_bool implementation](meltygui/view/control_view.py) illustrates typed input,
injected events, draw-list drawing and the changed/value contract; older comments
or incidental shortcuts in existing implementations do not override this guide.

Put local constants where the function's editor will find them, usually near its
top, and use the existing tint metadata conventions. Shared settings live in
`Toggles`, accessed through fully spelled-out attribute chains. Do not alias
settings namespaces or use string-based `getattr` fallbacks. `Toggles` and `Tint`
are the existing configuration/style mechanisms; they are not a reason to hide
changing feature state in globals.

Use `Tint` functions for text and other content on dynamic backgrounds. Fixed
design colours can be literal RGB tuples in the function. Keep icons as literal
f-strings, give variables descriptive names, and write comments that explain how
to change the code at the relevant point.

The same drawing, naming and style rules apply to raw-imgui overlays. Their input
and lifecycle integration belongs to the responsible core mechanism.

## Lifecycle, resources and background work

Make ownership explicit: distinguish the shared data source, each view's state,
and any disposable runtime resources. Two views of the same model should share
data deliberately and keep their local interaction state independent.

- Consume completed background work before drawing and dispatch requested work
  afterwards. Keep long-running work and worker ownership out of render bodies.
- Call nested and native window lifecycles every frame, passing `open_requested`.
  Do not create a window only in the click branch. Both backends must behave alike.
- Keep GL operations on the render thread; `ShaderRegistry` is not thread safe.
  A conversion path that runs without imgui or on a worker must not assume a GL
  context. Register cleanup with the existing lifecycle so held resources and
  references are released when their owner closes.
- Detect stale data using mtime, generations or identity as appropriate. Never
  hash or compare whole file contents merely to detect staleness.
- Preserve the framework's subprocess contract: full-path executables,
  `close_fds=False`, and no `cwd`, `preexec_fn` or `start_new_session` on
  `subprocess` calls. Follow the existing `posix_spawn` pattern where session or
  PTY setup is required; do not fork the running application.

## Reorganizing existing code

Start by identifying the value contract, state owners, render functions and
lifecycle hooks. Move pure presentation, model operations and state types to
their feature files; keep shared integration in core. Preserve behavior while
separating ownership. A file move alone does not complete this work if the view
still reaches back into a mixed module for its state and implementation.

Source edits must hotswap wherever the definitions live, including core. Preserve
existing function/class identities, held model values, callbacks and live state;
an unchanged source default must not overwrite its runtime value. Changes to a
source default should still take effect through the hotswap machinery.

Use the existing relocation support rather than feature-specific adoption code:

- [Definition relocation](meltygui/core/definition_hotswap.py) handles definitions
  moved between modules, including consumers and injected-state metadata.
- [Module compatibility](meltygui/core/module_compatibility.py) and
  [its manifest](meltygui/core/legacy_modules.json) make historical imports share
  the canonical module. Its whole-module adoption rebases source filenames and
  relies on preserved source line layout. A split or rewrite needs definition
  relocation/hotswap, not that assumption.
- [Saved-name mappings](meltygui/core/module_map.json) and source navigation must
  follow the same destinations. New code uses canonical imports; compatibility
  namespaces contain no second implementation or duplicate runtime registry.

Preserve unrelated work and application/session data. The
[migration inventory](docs/ARCHITECTURE_DEBT.md) records deferred areas; an adjacent
cleanup is not a reason to silently include the text-editor refactor.

## What to include in a contribution

Explain the concrete behavior, where the value and state live, and which existing
core mechanism supplies the view's dependencies. For a structural change, show
the before/after ownership and any remaining mixed responsibilities.

Validate the contracts affected by the change: mutable identity and edit
propagation, typed-value routing, independent view state, cached event delivery,
resource cleanup, saved values and live hotswap. Use focused tests that exercise
behavior, then broader checks when the shared surface warrants them. State which
GPU or backend paths were exercised; do not imply CPU/import checks validate
CUDA/GL behavior.

Verify changed UI behavior in an isolated app instance with its own session and
save paths. Follow the desktop-reservation instructions on Lukas's machine.
Setup, commands and release validation are in [Development](docs/DEVELOPMENT.md).
New runtime code must import through `meltygui` and work without the original
latent-descent checkout or `sys.path` modifications.
