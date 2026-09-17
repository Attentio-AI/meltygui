# Architecture gaps and migration inventory

Reviewed 2026-09-16 in the standalone MeltyGUI repository. This is a dated work
inventory, not the contribution standard; the standard is
[Contributing](../CONTRIBUTING.md). Findings below come from current call sites and
ownership, not just filenames or imports. They identify architectural coupling;
they do not imply that every listed feature is visibly broken.

Tensor data operations and camera math now live in models. Shared presentation,
dimension pickers and axis drawing live with tensor views, with runtime scale,
styling and fonts injected. CUDA contexts and compilation caches now have core owners; feature CUDA
presentation and demos have separate homes. The text-editor refactor remains explicitly deferred.

## What the migrations have established

- Reusable render functions are grouped as plain functions in 28 feature view
  modules. Core runtime modules, including modes, Melty, render execution,
  generic conversion and persistence, now have a common home in `core/`.
- Generic [RenderHost](../meltygui/core/conversion/render_host.py),
  [DictConversion](../meltygui/core/conversion/dict_conversion.py), event injection and
  definition relocation are available to reuse.
- `TensorDim`, `TensorDims` and `Lut` preserve primitive behavior while selecting
  specialized renderers. `FileMetaProxy` provides a shared dict-like store. These
  are useful value contracts even where their surrounding wiring needs work.
- `views/`, `widgets/`, `rendering/` and `windows/` have been removed. The
  library, Pro components and editor use canonical imports without aliases or
  shims. Saved identifiers translate during session loading; definition hotswap
  preserves live state independently of import compatibility.

These are placement and infrastructure milestones. Many functions in `view/`
still access shared runtime state directly, and some feature helpers moved into
core still need a responsibility split.

As a layout snapshot, legacy folders contain 77 non-`__init__` Python modules,
with four more implementation modules at the package root. This includes the
15 deferred editor modules. Examples, resources and the GNOME extension are
excluded. This count is not a compliance score: correctly named files can still
have the wrong dependencies.

## Tensor rendering: production views separated from demos

[tensor_model.py](../meltygui/model/tensor_model.py) owns slicing, dtype/axis
handling, neural-flow transforms, shape arithmetic and the strided volume adapter.
Pure camera math lives in [camera_model.py](../meltygui/model/camera_model.py).
These operations do not load views or CUDA rendering.

[voxel_view.py](../meltygui/view/voxel_view.py) now owns `draw_voxels`, the GL and
CUDA presentation passes, camera interaction, outlines and label billboards.
Shared dimension pickers, slice controls, tensor metadata and error cards remain
in [tensor_view.py](../meltygui/view/tensor_view.py). Direct sibling dimension
coordination remains intentional: noncolliding axis pickers form a reusable
local data shape, without tying the view to an application's root schema.

[graph_view.py](../meltygui/view/graph_view.py) owns its line shader and drawing;
[graph_model.py](../meltygui/model/graph_model.py) owns series slicing, packing and
ranges. Both views use texture upload/cache helpers in
[texture_model.py](../meltygui/model/texture_model.py). Their full-screen image
copy is shared presentation in [texture_view.py](../meltygui/view/texture_view.py).

[voxel_state.py](../meltygui/state/voxel_state.py) holds each voxel view's error
history and controls-panel state. Core injects keyboard availability and pointer
button state; voxel rendering no longer reads Melty's keyboard focus directly or
polls ImGui input. Shared settings still use the documented `Toggles` paths.

Palette values and generation live in [lut_model.py](../meltygui/model/lut_model.py).
`TextureId` remains the integer-like lazy GPU resource interface; `LutPalette`
exposes editable RGB lists through a dictionary. [lut_core.py](../meltygui/core/graphics/lut_core.py)
injects `Melty.luts` and connects edits to cached consumers, without a palette host.

[tensor_core.py](../meltygui/core/graphics/tensor_core.py) owns source publication
identity and cleanup integration. Text baking now uses the caller's `GLState`
for its private layout context, shader, VAO and buffers. This fixes the previously
observed cross-context VAO failure in multi-window voxel labels and gives those
resources the existing context cleanup lifecycle.

The old production `tensor/voxel_playground.py` and `core/graphics/graph_core.py`
are removed. Demo tensors and explicit window composition live in
[examples/voxel_playground.py](../examples/voxel_playground.py); importing the
production views creates no hosts or windows. No import shims were added.
See [the extraction and verification record](TENSOR_RELOCATION.md).

CUDA-to-GL uploads now live in [cuda_texture_model.py](../meltygui/model/cuda_texture_model.py):
versioned tensors become `GLTexture` values, with each texture/PBO/registration
owned by the supplied `GLState`. Shared context selection, registration, copying
and diagnostics live in [cuda_interop_core.py](../meltygui/core/graphics/cuda_interop_core.py),
with context ownership now shared through `core/graphics/cuda_context_core.py`
and retained runtime state on `Melty.cuda_interop`. Standalone uploads initialize the GL
device lazily; unsupported or incompatible contexts return the existing fallback
signal. Torch and CUDA-library imports remain optional until the adapter is used.

Partial allocations are cleaned up, mappings unmap on copy failure, and CUDA
unregistration must succeed before the GL buffer is deleted. The GL deletion
queue now supports explicit deferred retries. The adapter waits for the actual
producer device before copying, and restores caller GL bindings and CUDA context.
Direct tensor views retain their existing in-place CUDA rendering policy; this
adapter is also usable explicitly by passing its `GLTexture` to a view.

Remaining work:

- CUDA feature code now lives in `view/voxel_cuda_view.py` and
  `view/graph_cuda_view.py`; shared compilation/cache ownership lives in
  `core/graphics/cuda_kernel_core.py`, and dtype decoding in
  `model/cuda_tensor_model.py`. `tensor/` is removed.
- The mutation notification bug is fixed: core now honors caller `changed=True`
  before the cache decision even when the view does not declare a `changed`
  parameter. Live H100 voxels and RTX lines refresh immediately after mutation,
  then resume cache hits with the same kernels and contexts. This is explicit
  change notification, not polling or hashing tensor contents.
- The old `cuda_march` view parameter still does not choose the backend; reconcile
  that control with the intentional CUDA-residency policy separately.
- Review the controls popover's gesture-time lifecycle separately. This extraction
  preserves its existing behavior; it does not claim every interaction follows
  the final native-window lifecycle pattern.
- Investigate the earlier raw `imgui.same_line()` cache-layout probe: stale graph
  positions and partial updates appeared despite correct GPU readback. The
  stacked layout passed on both backends.

## Terminal: runtime separated; remaining behavior checks

`core/services/terminal_runtime.py` is injected per screen and owns focus,
startup/resize, input forwarding and output invalidation. The terminal model
publishes to weak subscribers so multiple views no longer overwrite a single
`term._ds` pointer. Rendering uses immutable screen snapshots and local
`DictConversion` selection/scroll state. Pointer/wheel inputs are declared.
Formatting and link geometry live in `view/terminal_view.py`. A narrow weak
notification bridge preserves already-running legacy reader frames while new
terminals have no `_ds` field. Live migration tests retain the real PTY, screen,
lock, observers and local state while held callbacks adopt edited source.

Native and GLFW direct-root windows pass typing and PTY output. The inline test
harness exposed a separate clipping/layout issue; scrollback-copy behavior also
remains outside this pass. These are not claims that every terminal interaction
has been revalidated.

## Files: injected metadata; persistence workers remain coupled

File renderers receive `file_metadata`, supplied by core only when declared;
explicit dictionaries, including empty dictionaries, override the shared store.
Nested listings, breadcrumbs and selectors pass that value through. Directory
scanning belongs to `model/file_model.py`; tint/order/drop operations over plain
mappings belong to `model/file_metadata_model.py`; watcher ownership and resize
diagnostics belong to `core/files/file_explorer_core.py`. Watch subscriptions
survive transfer from the old module registry.

`models/file_meta.py` retains its dict-like adapters, shared store and persistence
workers. Its legacy polling loop cannot stop cooperatively, so moving worker
ownership would strand an already-active frame during hotswap. That extraction
was deliberately deferred. Reload now preserves held entry identities and local
deletions when merging an external writer.

Remaining file helpers still mix focus, popovers, styles and keyboard handling;
file-tree service lookup and runtime diagnostics need further separation. Moving
these entry points does not complete the feature's architecture.

## Chat presentation still lives partly in the chat service package

Local wrapping, selection, previews and layout helpers live with chat views.
`view/chat_decoration_view.py` now owns buttons, cards, carets, activity dots,
hit testing and tint helpers. Scale, pointer position and drawing channel are
supplied by the caller. `model/chat_model.py` owns conversation ordering/activity
operations and tint access over supplied metadata. Chat navigation and transcripts
receive the same explicitly supplied metadata as file views.

`chat/chat_interface.py` still owns text measurement caches, title/prose/image
presentation, viewport behavior and other mixed helpers. Higher-level chat views
still discover focus, input and runtime services through Melty. Provider/session
integration and remaining `chat/`, `accounts/` and `completion/` modules need
further classification. Offline sidebar painting, selection and folder folding
pass; no provider/network conversation was started for verification.

## Shared state and code conversion remain mixed

[state/new_core_model.py](../meltygui/state/new_core_model.py) combines `DrawState`
with `TextEditorState`, `TabState`, `ColorPickerState`, `DropDownState`,
`MenuBarState`, `ZoomState` and shared enums. Core state and feature state do not
yet have distinct homes. Moving the whole file into core would keep that mixture.

[code/new_converters.py](../meltygui/code/new_converters.py) still combines source
loading/saving and recompilation, conversion, background coordination, state
types and rendering integration. Generic `RenderHost` and conversion machinery
have moved to core, but that does not make every remaining source-specific
operation core. Code adapters belong with the code model; presentation belongs
with the code views; common execution and lifecycle belong in core.

The text-editor implementation, editor-owned state and the mixed `DrawState`
split require the separately planned editor refactor. Keep this work out of the
tensor cleanup. Import paths have been updated mechanically; editor behavior
and the deliberate editor/state redesign remain outside this migration.

## Legacy helpers and core files need classification, not bulk moves

[utils/render_utils.py](../meltygui/utils/render_utils.py) combines older drawing
controls, window/frame wrappers, style manipulation and diagnostics. Active pure
presentation belongs with feature views; shared frame/window/debug integration
belongs in core. Determine which entry points still have callers before removing
or replacing them.

The remaining `graphics/` modules and root `hdr_color.py`, `image_load.py`,
`pbr.py` and `text_index.py` likewise need review by responsibility. File count,
the presence of GL calls or the word “utility” is not an ownership decision.
The `debug/jump_to.py`, `files/file_selector.py` and `files/folder_files.py`
compatibility shims have been deleted. Folder window registration now lives in
`core/files/file_core.py`; the reusable file views remain in `view/file_view.py`.

Some feature-named `*_core.py` modules hold helpers inherited from whole-module
moves. Keep their shared coordination in core, but move any remaining local
presentation or data adaptation to its proper feature. Direct `Melty`/`Core`
access in a view is a useful review signal; inspect the actual use before deciding
whether it is runtime state, styling or an unused import.

Core is now grouped into responsibility folders, with input and event handling
together in `core/input/`. The obsolete `rendering/`, `views/`, `widgets/` and
`windows/` namespace shells and the import-alias loader have been removed.
Folder organization does not resolve the mixed ownership above.

## Order and evidence for the next contributions

Continue with metadata worker lifecycle and remaining app-coupled presentation
in bounded changes. Keep the editor/state redesign separate. Use the
[core guide](../meltygui/core/README.md) to find existing mechanisms before adding
new plumbing.

A feature is separated when its renderer can use a supplied value without
discovering global feature state or importing demo setup, edits reach the same
backing model, and multiple views have independent local state. Verify cached
interaction, cleanup, saved values, source navigation and live hotswap as affected.
Report any remaining coupling explicitly instead of marking a directory move as
full architectural compliance.

## Latest integrated verification

The CUDA, terminal, files/metadata and chat passes were checked together:
657 library tests and 402 subtests passed; Pro passed 212 tests with one skip.
A built wheel installed into a fresh environment outside the checkout imports
all migrated features without eagerly initializing optional CUDA support.
Native offline chat selection/folder folding and file navigation passed. Terminal
keyboard/output passed native and GLFW; live migration retains a running PTY.
CUDA kernels passed on all three GPUs; the subsequently fixed mutation
notification path is described above. Shared metadata polling was retained to preserve active stores.
