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

- Reusable render functions are grouped as plain functions in 27 feature view
  modules. Core runtime modules, including modes, Melty, render execution,
  generic conversion and persistence, now have a common home in `core/`.
- Generic [RenderHost](../meltygui/core/render_host.py),
  [DictConversion](../meltygui/core/dict_conversion.py), event injection and
  definition relocation are available to reuse.
- `TensorDim`, `TensorDims` and `Lut` preserve primitive behavior while selecting
  specialized renderers. `FileMetaProxy` provides a shared dict-like store. These
  are useful value contracts even where their surrounding wiring needs work.
- `views/`, `widgets/`, `rendering/` and `windows/` contain only compatibility
  initializers. Historical imports, saved classes and source navigation can
  follow canonical modules without duplicating live state.

These are placement and infrastructure milestones. Many functions in `view/`
still access shared runtime state directly, and some feature helpers moved into
core still need a responsibility split.

As a layout snapshot, legacy folders contain 84 non-`__init__` Python modules;
two are import-only shims. Four more implementation modules remain at the package
root. That is 86 implementation modules outside the four intended areas, including
15 deferred editor modules. Examples, resources and the GNOME extension are
excluded. This count is not a compliance score: correctly named files can still
have the wrong dependencies.

## Tensor rendering: data extraction complete, shared ownership next

[tensor_model.py](../meltygui/model/tensor_model.py) now owns slicing, dtype/axis
handling, neural-flow transforms and shape arithmetic, the strided volume
adapter, and typed primitive wrappers. Its operations can run without loading
tensor views, the CUDA renderer or the playground. Pure camera math lives in
[camera_model.py](../meltygui/model/camera_model.py).

Tensor descriptions, viewport sizing, notices, metadata, dimension pickers and
axis presentation now live in [tensor_view.py](../meltygui/view/tensor_view.py),
shared with graph/input consumers. Axis label shaders and their per-view GL
resources belong to this presentation code; resource lifetime still uses the
injected `GLState`. Old imports remain aliases, and live relocation preserves
existing objects. See [the extraction and verification record](TENSOR_RELOCATION.md).

[tensor/voxel_playground.py](../meltygui/tensor/voxel_playground.py) is still a
production dependency, despite its name. It combines CUDA/GL upload and drawing,
LUT data and textures, slice controls, error presentation,
demo tensors, hosts and window
registration. Importing it also constructs demo hosts and registers their
windows.

[tensor_view.py](../meltygui/view/tensor_view.py) contains the main renderers but
imports much of that implementation. `draw_lut` looks for `lut_host` in its module
globals and otherwise falls back to `LUTS`; the host is created in
`voxel_playground.py`. That implicit lookup should become an explicit supplied
LUT collection. `draw_tensor_dim` uses its local collection and immediate parent
to coordinate sibling axis selections. That is permitted local view behavior
and was preserved when the controls moved into feature views.
Noncolliding dimension pickers form a reusable local data shape: callers should
be able to repeat the picker in a loop without rebuilding its coordination.
The controls now receive scale and styling through core injection; axis label
rendering also receives its font through the injected font manager.

[graph_view.py](../meltygui/view/graph_view.py) also imports LUTs, the shared
`_LUT_TEXTURES` cache, volume uploads and error helpers from the voxel
module. A tensor cleanup must account for these consumers. Creating a second
graph-owned LUT cache would duplicate shared state.

The remaining split is:

- Make the shared LUT collection an explicit dependency and separate its model
  data from the shared texture cache. Preserve palette edits and cache identity.
- Move the remaining slice controls and error presentation into feature views.
  Use injected or automatically managed state and preserve local coordination.
- Give local interaction state and held resources clear owners. Reuse the
  existing injected GL state and cleanup mechanisms where they fit.
- Separate feature rendering/computation from shared CUDA context, GL resource
  and synchronization machinery. Do not move every GPU-related function into
  core merely because it is low level.
- Move demo data, hosts and sample windows into examples. Importing a reusable
  tensor renderer should not instantiate a playground.

[tensor_core.py](../meltygui/core/tensor_core.py) still contains only the voxel
cleanup hook. CUDA interop, CUDA marching and line kernels in `tensor/` need the
same ownership review. Preserve GPU residency, active-device behavior, output,
resource lifetime and hotswap; the tensor-specific CUDA/GL tests matter here.

## Terminal rendering still owns runtime integration

In [terminal_view.py](../meltygui/view/terminal_view.py), `draw_terminal_screen`
wires the model's invalidation target through `term._ds`, manages first-appearance
focus through `Melty.text_focused_ds`, starts/resizes the terminal and reads imgui
input state. Rendering, process lifecycle, focus and input delivery are coupled.
It also imports many helpers and configuration values from
[terminal_core.py](../meltygui/core/terminal_core.py).

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
tensor cleanup. Preserve existing imports through compatibility where necessary.

## Legacy helpers and core files need classification, not bulk moves

[utils/render_utils.py](../meltygui/utils/render_utils.py) combines older drawing
controls, window/frame wrappers, style manipulation and diagnostics. Active pure
presentation belongs with feature views; shared frame/window/debug integration
belongs in core. Determine which entry points still have callers before removing
or replacing them.

The remaining `graphics/` modules and root `hdr_color.py`, `image_load.py`,
`pbr.py` and `text_index.py` likewise need review by responsibility. File count,
the presence of GL calls or the word “utility” is not an ownership decision.
`debug/jump_to.py` and `files/file_selector.py` are already import-only shims;
they do not represent two more implementations to extract.

Some feature-named `*_core.py` modules hold helpers inherited from whole-module
moves. Keep their shared coordination in core, but move any remaining local
presentation or data adaptation to its proper feature. Direct `Melty`/`Core`
access in a view is a useful review signal; inspect the actual use before deciding
whether it is runtime state, styling or an unused import.

## Order and evidence for the next contributions

Resume with tensor models and views, including the graph consumers of their
helpers. Then apply the same ownership review to terminal, file and chat features
in bounded changes. Keep the editor/state redesign separate. Use the
[core guide](../meltygui/core/README.md) to find existing mechanisms before adding
new plumbing.

A feature is separated when its renderer can use a supplied value without
discovering global feature state or importing demo setup, edits reach the same
backing model, and multiple views have independent local state. Verify cached
interaction, cleanup, saved values, source navigation and live hotswap as affected.
Report any remaining coupling explicitly instead of marking a directory move as
full architectural compliance.
