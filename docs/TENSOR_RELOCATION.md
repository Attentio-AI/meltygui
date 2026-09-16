# Tensor extraction: data and shared presentation

Implemented in bounded batches on 2026-09-16; remaining ownership work
is tracked in [Architecture gaps](ARCHITECTURE_DEBT.md#tensor-rendering-shared-palettes-extracted-runtime-and-demos-remain).

| Destination | Responsibility moved |
|---|---|
| [model/tensor_model.py](../meltygui/model/tensor_model.py) | Fourteen definitions from `voxel_playground`: dtype/axis handling, slicing, strided volume data, neural flow, volume proportions and tensor detection. Also owns `nf_display_shape`, formerly in `cuda_march`. |
| [model/camera_model.py](../meltygui/model/camera_model.py) | The complete pure camera-math module, formerly `tensor/voxel_camera.py`. |
| [view/tensor_view.py](../meltygui/view/tensor_view.py) | Five shared helpers for descriptions, viewport sizing, image notices, byte formatting and metadata overlays. Graph/input consumers now import these directly. |

The no-copy slicing path no longer imports the CUDA renderer for shape arithmetic.
Model operations run without importing tensor/graph views or constructing demo
hosts. Algorithms, tensor storage behavior and rendering output are unchanged.

Old definition imports remain re-exports. The old camera path shares the canonical
module through the common compatibility layer; saved module names point to it.
The existing generic definition relocation machinery preserves held functions,
classes and instances. No tensor-specific hotswap path was added.

Verification:

- 51 focused tests passed: tensor models, shaped dispatch and core relocation.
- 39 CUDA/GL tests passed: CUDA marching, interop, direct line sampling and camera
  roll rendering, using a reserved agent desktop.
- A live migration replay loaded the pre-move source, retained functions/classes,
  a volume instance, hosts and LUT values, then applied the edited modules. All
  retained identities, data results and canonical source addresses survived.
- An isolated app rendered CUDA and GL tensor volumes, metadata overlays and a
  CUDA graph without runtime errors. The test app used separate session paths.
- Deferred editor sources were unchanged.

## Dimension controls and axis drawing

A second batch moves twelve more helpers and twelve supporting constants/shaders
from `voxel_playground.py` into `tensor_view.py`: the dimension picker helpers,
axis projection, tick placement, outlines, billboard layout, label shaders, atlas
and instanced drawing. Graph tick rendering imports its shared helper directly.

Sibling axis swaps remain local view behavior. A picker uses its immediate
parent and local collection; callers can reuse the coordinated controls in a
loop. No app model or additional caller-side coordinator is required.

Core now injects `ui_scale` and `font_manager` when requested, alongside the
existing `style_manager`. These are runtime dependencies, excluded from saved
parameters and parameter controls. The picker uses local drawing depth and
supplied scale/style; the label atlas uses a supplied font. Its textures, shader
and buffers retain their existing per-view `GLState` ownership and cleanup.

Verification of this batch:

- 75 focused model, control, dispatch, relocation and hotswap tests passed.
- 41 rendering/injection tests passed, including all 39 tensor CUDA/GL tests.
- A process loaded the old definitions before the source edits, then applied the
  migration live. Functions, renderers, hosts, LUT values and the texture cache
  retained identity; sibling swapping, projected axes and ticks stayed correct.
- Isolated native Wayland and GLFW apps each rendered two independent picker
  groups plus CUDA and GL tensor labels. Clicking a conflicting axis swapped
  only its sibling in the same group; the other group stayed unchanged.
- Deferred editor sources were unchanged.

## Shared palette ownership

- `model/lut_model.py` owns `Lut`, palette generation, editable RGB lists in
  `LutPalette`, and lazy `LutTexture` values.
- `model/texture_model.py` provides `TextureId`: an integer-like proxy whose GPU
  storage belongs to the current rendering context.
- `view/lut_view.py` owns the picker and swatch render functions.
- `core/graphics/lut_core.py` injects `Melty.luts` and subscribes consumers before the
  cache gate; callers can supply their own palette. No RenderHost or separate
  `lut_resources` service is involved.

Both tensor and graph views use `luts.texture(name)` as a texture ID. The proxy
handles upload and cleanup behind that interface; CUDA uses its matching tensor.
CUDA previously read baked defaults, and some upload keys missed colour edits
that kept the same length. Both GL and CUDA now refresh on those edits and share
uploads within each context and palette collection. Surface cleanup releases
that context's resources. Migration preserves edited lists, allocations and the
legacy texture-cache dictionary while retiring the old palette host.

Verification covers lazy construction without a host, direct PyOpenGL binding,
same-length edits, allocation adoption and context cleanup. In isolated native
Wayland and GLFW apps, a shared red-to-blue palette edit refreshed cached
CUDA/GL volumes and graphs without hovering them.

Live `#[...]` parameters still feed `draw_voxels`. Comment changes are now checked
before the cache gate, so changing the parsed annotation updates an existing
view immediately. Whole-module hotswap also backfills missing literal constructor
defaults in tracked state objects without rerunning constructors or overwriting
live values. Both have regression coverage; the text editor remains untouched.

Next: remaining slice controls and error state; feature drawing versus generic
GPU lifecycle; demo hosts and windows moved out of library dependencies.
`voxel_playground.py` remains a production dependency until those responsibilities
are separated.

## Slice controls, error state and graph helpers

- `control_view.py` provides `draw_int_slider`, a draw-list control driven by
  injected pointer events. Tensor and graph slice rows share `draw_tensor_slices`
  in `tensor_view.py`; edits use the existing parameter invalidation path.
- `draw_tensor_error` renders supplied error text with an injected
  `TensorErrorState`. The old voxel helpers remain compatibility imports.
- `model/graph_model.py` owns line-axis resolution, slicing, packing and finite
  ranges. Graph coordinate/tick helpers and axis overlays live in `graph_view.py`.
- Inline headers no longer subtract a header row from the content hit rectangle;
  this fixes injected pointer delivery to the visible value row.

A live native app verified slider and slice edits, volume rendering and the
shared error card. Applying the subsequent core folder moves preserved its
edited values, LUT proxy and GLState. The remaining voxel GPU/runtime/demo
coupling is tracked in the architecture inventory.
