# Tensor extraction: data and shared presentation

Implemented in bounded batches on 2026-09-16; remaining ownership work
is tracked in [Architecture gaps](ARCHITECTURE_DEBT.md#tensor-rendering-production-views-separated-from-demos).

| Destination | Responsibility moved |
|---|---|
| [model/tensor_model.py](../meltygui/model/tensor_model.py) | Fourteen definitions from `voxel_playground`: dtype/axis handling, slicing, strided volume data, neural flow, volume proportions and tensor detection. Also owns `nf_display_shape`, formerly in `cuda_march`. |
| [model/camera_model.py](../meltygui/model/camera_model.py) | The complete pure camera-math module, formerly `tensor/voxel_camera.py`. |
| [view/tensor_view.py](../meltygui/view/tensor_view.py) | Five shared helpers for descriptions, viewport sizing, image notices, byte formatting and metadata overlays. Graph/input consumers now import these directly. |

The no-copy slicing path no longer imports the CUDA renderer for shape arithmetic.
Model operations run without importing tensor/graph views or constructing demo
hosts. Algorithms, tensor storage behavior and rendering output are unchanged.

Callers import the feature modules directly; temporary re-exports and the
module-alias layer have been removed. Saved names still translate on load.
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

Those follow-up extractions are recorded below; the playground production
dependency has now been removed.

## Slice controls, error state and graph helpers

- `control_view.py` provides `draw_int_slider`, a draw-list control driven by
  injected pointer events. Tensor and graph slice rows share `draw_tensor_slices`
  in `tensor_view.py`; edits use the existing parameter invalidation path.
- `draw_tensor_error` renders supplied error text with an injected
  `TensorErrorState`. Tensor and graph consumers import these views directly.
- `model/graph_model.py` owns line-axis resolution, slicing, packing and finite
  ranges. Graph coordinate/tick helpers and axis overlays live in `graph_view.py`.
- Inline headers no longer subtract a header row from the content hit rectangle;
  this fixes injected pointer delivery to the visible value row.

A live native app verified slider and slice edits, volume rendering and the
shared error card. Applying the subsequent core folder moves preserved its
edited values, LUT proxy and GLState. The subsequent voxel ownership split is recorded below.


## Voxel production path and explicit playground

- `view/voxel_view.py` owns the voxel renderer, GL raymarch shader, CUDA image
  rendering, camera interaction, and axis/label presentation. Shared tensor
  controls stay in `view/tensor_view.py`; graph shaders join `view/graph_view.py`.
- `model/texture_model.py` owns the shared CUDA image transfer and cached volume
  lookup. `view/texture_view.py` owns the image blit pass. Existing resource keys,
  tensor storage and shader algorithms are retained.
- `state/voxel_state.py` separates each view's CUDA error, label warning and panel
  state. A successful render in one view cannot clear another view's error.
  Existing panel state is adopted from that view's old saved `misc` entry.
- `core/graphics/tensor_core.py` owns publication-generation cache identity and
  teardown integration. Core supplies input context instead of views reading
  global keyboard focus or polling pointer buttons.
- `core/graphics/text_texture.py` accepts the caller's GLState. Its layout context
  and GPU pipeline are local to that owner and released with it. This fixes
  labels using a VAO from another native window's context.
- `examples/voxel_playground.py` explicitly opens torus, 4-D, 5-D, neural-flow and
  line demos. Production imports create no demo tensors, hosts or windows.
  The two former mixed production/demo modules are deleted, without shims.

Validation includes unchanged shader-source comparison, CUDA/GL output and HDR
checks, a real CUDA render through the wrapper, repeated resource reuse and
cleanup, per-view error isolation, and text baking across two GL contexts.
A whole-module source edit at the new voxel path preserves the held render
function, edited camera state, injected state and CUDA output allocations while
applying a changed camera default. Live-comment override regression tests remain
part of the suite. Multi-window playground checks exercise the repaired labels.


## CUDA interop ownership

- `model/cuda_texture_model.py` owns `CudaVolume` and `tensor_to_texture`: a
  versioned tensor becomes a renderable `GLTexture`; its source tensor is not
  retained by the allocation. Cache hits avoid packing, transfers and syncs.
- `core/graphics/cuda_interop_core.py` owns display-device discovery, CUDA context
  setup, registration, mapping/copy/unmapping and context restoration. Retained
  context and diagnostics are on `Melty.cuda_interop`. Imports neither initialize
  CUDA nor require Torch. No old-module shim remains.
- The GL device comes from `cuGLGetDevices`, not an assumed device zero. Both the
  actual CUDA context and PyCUDA's recorded context must agree before registering.
  Upload and cleanup temporarily activate the registration's context, then restore
  the caller's native context, including when Torch has changed it independently
  of PyCUDA's stack.
- PyCUDA's inactive primary-context detach assumes release implicitly pops a
  native context, whereas CUDA explicitly leaves it on the stack. The core release
  helper balances that activation after the owned PyCUDA entry has been removed.
  It does not detach caller-owned contexts.
- Partial allocation failures queue cleanup without replacing the last good
  resource. Explicit `ResourceDeletionDeferred` retains failed unregistrations
  for the next GL deletion drain, preserving unregister-before-buffer-delete order.
  Existing queued cleanup callbacks see module edits through `_release_volume`.
- Producer synchronization names the tensor/device and happens before the driver
  copy. Cross-device tensors are staged by Torch; raw copies stay on one device.
  GL array/unpack-buffer and texture bindings are restored on success and failure.

Verification covers float16/float32, noncontiguous input, all three local GPUs,
nondefault producer streams, cold standalone setup, two GL contexts, conflicting
native/PyCUDA contexts, failed allocation/copy/upload/unregistration, versioned
reuse (including `None`), and hotswap preserving runtime and resource identities.
An isolated app rendered a GLTexture uploaded from H100 `cuda:2` to display
GPU `cuda:1`; live tensor updates reused the same texture and PBO.
The full library suite passed 623 tests plus 393 subtests.
