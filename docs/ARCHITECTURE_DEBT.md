# Architecture gaps and migration inventory

Reviewed 2026-09-16 in the standalone MeltyGUI repository. This is a dated work
inventory, not the contribution standard; the standard is
[Contributing](../CONTRIBUTING.md). Findings below come from current call sites and
ownership, not just filenames or imports. They identify architectural coupling;
they do not imply that every listed feature is visibly broken.

Tensor data operations and camera math now live in models. Shared presentation,
dimension pickers and axis drawing live with tensor views, with runtime scale,
styling and fonts injected. Shared runtime ownership and demo separation remain
open. The text-editor refactor remains explicitly deferred.

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

As a layout snapshot, legacy folders contain 79 non-`__init__` Python modules,
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
with retained runtime state on `Melty`. Standalone uploads initialize the GL
device lazily; unsupported or incompatible contexts return the existing fallback
signal. Torch and CUDA-library imports remain optional until the adapter is used.

Partial allocations are cleaned up, mappings unmap on copy failure, and CUDA
unregistration must succeed before the GL buffer is deleted. The GL deletion
queue now supports explicit deferred retries. The adapter waits for the actual
producer device before copying, and restores caller GL bindings and CUDA context.
Direct tensor views retain their existing in-place CUDA rendering policy; this
adapter is also usable explicitly by passing its `GLTexture` to a view.

Remaining work:

- Classify CUDA marching and line kernels still in `tensor/`:
  separate feature computation from shared context and synchronization machinery.
  Preserve GPU residency, device behavior, output, resource lifetime and hotswap.
  The old `cuda_march` view parameter currently does not choose the backend;
  reconcile that control with the intentional CUDA-residency policy separately.
- Review the controls popover's gesture-time lifecycle separately. This extraction
  preserves its existing behavior; it does not claim every interaction follows
  the final native-window lifecycle pattern.
- Investigate the earlier raw `imgui.same_line()` cache-layout probe: stale graph
  positions and partial updates appeared despite correct GPU readback. The
  stacked layout passed on both backends.

## Terminal rendering still owns runtime integration

In [terminal_view.py](../meltygui/view/terminal_view.py), `draw_terminal_screen`
wires the model's invalidation target through `term._ds`, manages first-appearance
focus through `Melty.text_focused_ds`, starts/resizes the terminal and reads imgui
input state. Rendering, process lifecycle, focus and input delivery are coupled.
It also imports many helpers and configuration values from
[terminal_core.py](../meltygui/core/services/terminal_core.py).

Keep the terminal model's PTY/screen behavior behind its value interface. Let core
own subscription, focus, invalidation and lifecycle integration, and supply the
screen view with its local selection state and input. Pure terminal formatting
and link handling should be classified separately from process/session wiring.
This needs behavioral checks for cached redraws and input; a rename alone is
insufficient.

## File views still acquire shared services themselves

[file_model.py](../meltygui/model/file_model.py) already separates filesystem
reconciliation and metadata adaptation from drawing. However,
[file_view.py](../meltygui/view/file_view.py) still calls `file_meta_store()` inside
renderers and reaches into Melty's code cache, channels and style machinery.
The diagnostics in that module also combine collection of runtime information
with its presentation.

Supply metadata/model values and rendering services through the reusable
framework interface. Keep scanning, reconciliation and applying file changes in
the model, and subscriptions/shared ownership in core. The dict-like
[file metadata store](../meltygui/models/file_meta.py) is a useful adapter but
still combines its value behavior with persistence workers and global repaint
coordination. Preserve its shared object identity and saved-data behavior while
separating those responsibilities.

## Chat presentation still lives partly in the chat service package

Fourteen local presentation helpers now live with chat views: wrapping, selection,
message labels/previews, tint calculation, row geometry and scroll anchoring.
Account field rendering also lives in `account_view.py` and receives its backing
store explicitly.

[chat_view.py](../meltygui/view/chat_view.py) imports drawing helpers such as
`_button`, `_card`, `_caret` and `_hovering` from
[chat/chat_interface.py](../meltygui/chat/chat_interface.py). The old module also
imports shared file metadata and runtime/layout facilities.

Keep reusable message/card presentation with the chat views. Put message/value
adaptation and provider behavior behind the model interface, per-view interaction
state with the chat state, and shared session/task integration in core. Review the
remaining `chat/`, `accounts/` and `completion/` modules by role; they are not all
views or all core merely because the feature uses asynchronous services.

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

Continue with the remaining tensor backend ownership and app-coupled presentation
in terminal, file and chat features
in bounded changes. Keep the editor/state redesign separate. Use the
[core guide](../meltygui/core/README.md) to find existing mechanisms before adding
new plumbing.

A feature is separated when its renderer can use a supplied value without
discovering global feature state or importing demo setup, edits reach the same
backing model, and multiple views have independent local state. Verify cached
interaction, cleanup, saved values, source navigation and live hotswap as affected.
Report any remaining coupling explicitly instead of marking a directory move as
full architectural compliance.
