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

As a layout snapshot, legacy folders contain 84 non-`__init__` Python modules;
two are import-only shims. Four more implementation modules remain at the package
root. That is 86 implementation modules outside the four intended areas, including
15 deferred editor modules. Examples, resources and the GNOME extension are
excluded. This count is not a compliance score: correctly named files can still
have the wrong dependencies.

## Tensor rendering: shared palettes extracted, runtime and demos remain

[tensor_model.py](../meltygui/model/tensor_model.py) now owns slicing, dtype/axis
handling, neural-flow transforms and shape arithmetic, the strided volume
adapter, and typed primitive wrappers. Its operations can run without loading
tensor views, the CUDA renderer or the playground. Pure camera math lives in
[camera_model.py](../meltygui/model/camera_model.py).

Tensor descriptions, viewport sizing, notices, metadata, dimension pickers and
axis presentation now live in [tensor_view.py](../meltygui/view/tensor_view.py),
shared with graph/input consumers. Axis label shaders and their per-view GL
resources belong to this presentation code; resource lifetime still uses the
injected `GLState`. Imports use the feature modules directly, and definition
hotswap preserves existing objects. See [the extraction and verification record](TENSOR_RELOCATION.md).

Slice controls and error panels now also live in tensor views. The slices use
the injected-event integer slider in `control_view.py`; error history uses
`state/tensor_state.py`. Graph series slicing, packing and ranges live in
`model/graph_model.py`, and graph axis presentation lives in `graph_view.py`.

[tensor/voxel_playground.py](../meltygui/tensor/voxel_playground.py) is still a
production dependency, despite its name. It combines CUDA/GL upload and drawing,
demo tensors, hosts and window
registration. Importing it also constructs demo hosts and registers their
windows.

[tensor_view.py](../meltygui/view/tensor_view.py) contains the main renderers but
imports much of that implementation. `draw_tensor_dim` uses its local collection
and immediate parent
to coordinate sibling axis selections. That is permitted local view behavior
and was preserved when the controls moved into feature views.
Noncolliding dimension pickers form a reusable local data shape: callers should
be able to repeat the picker in a loop without rebuilding its coordination.
The controls now receive scale and styling through core injection; axis label
rendering also receives its font through the injected font manager.

Palette values and generation now live in [lut_model.py](../meltygui/model/lut_model.py),
and the picker and swatches in [lut_view.py](../meltygui/view/lut_view.py).
[TextureId](../meltygui/model/texture_model.py) provides the integer-like texture
interface; `LutTexture` owns the lazy GL/CUDA uploads and `LutPalette` exposes
editable RGB lists through a dictionary. [lut_core.py](../meltygui/core/graphics/lut_core.py)
injects `Melty.luts` and connects edits to cached consumers. No palette host or
separate resource service is needed. Migration retires the old host while keeping
edited lists and existing allocations. Same-length edits refresh uploads, and
each GL context releases its resources on close.

[graph_view.py](../meltygui/view/graph_view.py) still imports volume uploads from
the voxel module. The remaining cleanup must include these
consumers, while keeping palette ownership shared.

Two separate UI probes need follow-up: raw `imgui.same_line()` between cached
views produced stale graph positions and partial tile updates, although GPU
readback contained the new colours. A stacked layout refreshed correctly on
both backends. A GLFW probe with several OS roots also hit an invalid cached
VAO in `core/text_texture.py`; its global GL state needs a context-ownership
review. The palette verification used one OS window per backend.

The remaining split is:

- Give local interaction state and held resources clear owners. Reuse the
  existing injected GL state and cleanup mechanisms where they fit.
- Separate feature rendering/computation from shared CUDA context, GL resource
  and synchronization machinery. Do not move every GPU-related function into
  core merely because it is low level.
- Move demo data, hosts and sample windows into examples. Importing a reusable
  tensor renderer should not instantiate a playground.

[tensor_core.py](../meltygui/core/graphics/tensor_core.py) still contains only the voxel
cleanup hook. CUDA interop, CUDA marching and line kernels in `tensor/` need the
same ownership review. Preserve GPU residency, active-device behavior, output,
resource lifetime and hotswap; the tensor-specific CUDA/GL tests matter here.

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

Resume with tensor GPU/demo separation and the remaining app-coupled presentation
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
