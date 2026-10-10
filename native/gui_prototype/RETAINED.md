# Retained @gui experiment

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the full implementation guide,
feature matrix, development history and verification record.

Build and launch from the checkout:

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python examples/tile_manager.py --rust-cache
.venv/bin/python examples/tile_manager.py --rust-layout
```

The original `--rust-gui` comparison remains available. This second experiment
uses ordinary `@gui` functions under `@os_window` (the existing `@glfw_window`
host). No application code creates
a runtime or binds decorators. The existing window host owns a private native
dispatch/cache instance per OS surface. Production core_render, tile-cache
capture and window replay are not replaced.

```python
from meltygui import gui, os_window

@gui(width=400, height=100)
def content(draw_state=None):
    # Draw with the native state and normal injected arguments.
    pass

@os_window(name="Example", width=800, height=600)
def app():
    content()
```

`@os_window(...)` is shorthand for `@gui(glfw_window=True, ...)`. Both register
the GUI root with the existing window host, including its initial size, settings,
app identity and close callback. Plain `@gui` remains a reusable component. Both
decorators can be used without parentheses. Importing them does not open a
display or load the optional Rust extension; the native build is needed to draw.

Neither `input_value` nor a return statement is required for drawing-only views.
Returning `None` becomes `(False, input_value)`, preserving the caller's value
and identity. Explicit `(changed, value)` returns continue to deliver edits.
Typed state and arbitrary caller/definition arguments still work without an
`input_value` parameter.

The smallest app is [examples/example_ui.py](../../examples/example_ui.py):

```python
from meltygui import os_window

@os_window(name="example main", width=1400, height=1000)
def example_app():
    pass
```

The previous two-decorator form also works:

```python
from meltygui import glfw_window, gui

@glfw_window(name="Example", width=800, height=600)
@gui(use_cache=False)
def app():
    content()
```

`use_cache=False` is for immediate host UI such as the demo's live counters.
The root can instead use default `@gui` to retain its own output. Typed
`DictConversion` arguments still provide automatic per-view state injection.
The invalidation interface is supplied as the `cache` argument when requested.

## What to try

* **Invalidate number by path / ID:** changes only the number's background.
  The number body runs; owner and grandparent body counts remain fixed. Their
  textures are recomposited from retained draw commands referencing the child.
* **+ 1 / - 1:** edits return through the owner's ordinary call site and assignment.
  Both the numeric display and caller display settle to the edited value. Pending
  changes are consumed once; stale caller echoes cannot undo the live value.
* **Toggle conditional window:** mutates the controlling model and invalidates
  its associated owner. The owner executes its actual `if not closed` statement
  beneath a cached grandparent. Omitted windows lose textures, contexts, hit
  regions and shadows. Reopening declares a fresh window.
* **Close via owner:** same lifetime behavior, initiated from inside the window.
* **Drag the window title:** changes placement/z order; no cached body executes
  and no texture is recaptured. Idle frames also execute no cached bodies.
  The peer window can overlap it; clicking either raises it. Its local pulse
  changes only its own texture. **Toggle peer window** tests independent lifetime.

Hover transitions deliberately invalidate the affected control's pixels. They
therefore increment its body count. Returning a changed scalar can refresh its
caller after assignment, including text drawn before that assignment.

## Ownership and scheduling

`retained.rs` owns stable call identities, caller/input references, explicit
invalidation associations, dirty execution scheduling, result mailboxes, child
declaration reconciliation, portal placement/order and retirement. Object
associations use identity; path associations use the supplied string. Models
are never recursively hashed, compared or inspected for mutations. Mutable
changes use `(changed, value)` or `cache.invalidate(associated_thing)`.

Dirty nodes execute in ownership order. A node can execute using its retained
invocation without executing ancestors. Events are freshly injected, never kept
in saved caller arguments. Pending returned edits wake the caller and replay at
its real call site. Pixel-only invalidation marks ancestors for composition,
not Python execution. A dirty owner withdrawing a child takes precedence over
queued work for that child.

Successful owner executions replace their declaration set. A cache hit preserves
that set. An omitted declaration on an actual execution retires its whole owned
subtree. The window host applies the same rule to root calls automatically.
An exception preserves the owner's previously committed declaration set
and retires newly introduced declarations; it does not roll back Python model
mutations or modifications to already existing child nodes.

## Layout invalidation experiment

`--rust-layout` opens two views of the same model: one measured parent and one
fixed-height parent, plus a separately composed, measured-height owned window.
Add/remove rows, empty the content, change only its colour, narrow the width,
switch between vertical and horizontal flow, and hide/show the child or window.
The panel displays body-execution counts and current geometry. A cached sibling
can move without executing. The fixed parent's outer panel does not execute
when the child grows; its retained commands are recomposed with clipping.

`height=None` requests content measurement. `width=None` fills the remaining
width in the parent at the call position (or the available width in the host).
Explicit dimensions remain constraints. `min_height` and `max_height` clamp the
assigned extent; native `content_height` retains the measured content size.
An RGB/RGBA `tint` tuple supplies a background over the final assigned bounds;
the native compositor applies it after measurement, with correct nested alpha.
Raw draw-list primitives must reserve their extent with `imgui.dummy`, as in
ordinary ImGui layout. Empty content has logical height zero and a 1px backing
texture. ImGui measures item extents in whole pixels; explicit fractional
constraints are rounded upward. Measured capture is limited to 8192 logical px.

The sequence is:

1. Execute an invalidated body using its retained invocation and current width.
   An automatic-height body uses a large logical capture viewport so its old
   texture bounds cannot clip newly grown content. No large texture is allocated
just for measurement; user code executes once, not in separate measure/draw passes.
2. Measure its ImGui group, apply height bounds, resize the existing texture
   storage while retaining its texture name, and capture its new commands.
3. Rust compares the assigned extent with the previous extent. A real change
   invalidates the immediate inline owner for layout. An owner already executing
   consumes the new extent at the call site, so no redundant pass is requested.
   A portal occupies no inline space and does not wake its declaration owner.
4. The owner reruns its ordinary Python/ImGui layout using cached child extents.
   Clean siblings change placement without executing. Propagation continues only
   if the owner's own assigned extent changes. Pixel changes only mark composition.
5. After the graph settles, replay commands bottom-up and compose windows. Root
   calls settle before reserving space in the immediate host. A later mutation
   also requests a host frame. Hit testing uses the resulting geometry and clips.

The graph stores constraints separately from measured output, so calling a
measured child again does not mistake its old requested height for a size change.
Pending returned edits protect the input value, while new caller layout/style
arguments still take effect. A width change cannot be lost behind a result mailbox.
`cache.layout_log` keeps the last 64 extent changes. Non-converging invalidation
reports function names, IDs, geometry and recent changes. The pass limit scales
with graph size to allow valid deep chains.

This deliberately reruns the immediate owner when a child's extent changes:
arbitrary Python can use that extent to position siblings or generate drawing.
Independent layout-command replay would require a more restrictive layout API.
The experiment does not attempt to infer geometry dependencies between unrelated
views, or reads of portal geometry by its owner; those need explicit invalidation.
During an auto-height body, `draw_state.height` is the previous assigned height
(a provisional default on first use), not a prediction of its new extent. Use
content layout to determine height and `tint` for full-boundary backgrounds.
Self-referential layout based on the same view's unknown final height is not solved.

## Drawing path

The Python bridge gives each cached node a private ImGui layout context sharing
the host font atlas. On an invalidated execution it copies finalized ImGui vertex,
index and command buffers into Rust. This is command capture before composition,
not extraction from the already composed framebuffer.

`texture.rs` uploads and replays those commands directly through OpenGL into
private textures. Clipping is applied per command. Intermediate alpha is resolved
to straight alpha before an ordinary ImGui image samples the texture. Inline
child calls insert references to stable child texture names. Native bottom-up
command replay updates ancestor textures without reexecuting their functions.

Internal windows are ownership children but composition portals. A window's
texture is not baked into its owner. The small compositor presents the graph's
current window inventory in z order, including title and shadow. Closing an
owned window therefore cannot leave an independently replaying registry entry.

GPU work and cleanup remain on the rendering thread. Removed nodes release native
targets and Python layout contexts. OS-surface teardown closes the private GUI
owner before destroying the renderer and its shared font resources.
The default texture/scratch allocation budget is 256 MiB; exceeding it raises an
error rather than evicting textures still referenced by retained commands.

## Intentional limits

* This is a desktop OpenGL prototype. The host is the existing `@glfw_window`
  application. Internal windows use a small new compositor; production Melty
  docking and native child OS surfaces are not integrated. New Rust constraints,
  local resize gestures and native containment are described in
  [COLLISIONS.md](COLLISIONS.md). Passing `glfw_window=True` to a retained view explicitly fails.
* Width is constrained; height can be fixed, measured or bounded. There is no
  intrinsic-width solver, scrolling/virtualization or partial texture updates.
  Retained rows/columns and generic frozen resize replay are now available;
  portal positions are parent-relative. Capture resolution is one texel per
  logical pixel; per-monitor DPI transitions are not integrated.
* Pointer regions are retained by `cache.region` and injected as `view_events`.
  Raw ImGui widgets also receive host IO in their isolated contexts. Pointer
  activity invalidates the hit view rectangle; press capture survives leaving
  that rectangle; keyboard/characters go to the clicked context. Text editing
  and held interactions request frames. Idle unrelated views remain cached.
  Input destination and gesture ownership are separate: ImGui controls claim
  left drags, text/blank space allows background movement, and unclaimed normal
  or double-right drags reach the native resize system. Existing explicit
  regions/internal windows retain their claims. Cross-context Tab navigation,
  IME, OS-focus integration, cross-context drag/drop, accessibility and popups
  outside a retained texture remain gaps. See `examples/gui_imgui_widgets.py`.
* A private ImGui context/runtime per cached node and Python pointer routing make
  this a semantic experiment, not a production-scale implementation. Rust still
  scans node flags for scheduling; there is no incremental ready queue or spatial
  index yet. There is no cache eviction or GPU-resident command-buffer pool.
* Commands support this build's 20-byte vertices and 32-bit indices, ordinary
  ImGui texture commands and accumulated index offsets. Custom draw callbacks,
  nonzero vertex base offsets and arbitrary external texture lifetimes are not
  supported. Fonts/textures must outlive retained packets. Theme/font/atlas
  regeneration needs an explicit invalidation/resource-lifetime integration.
* Declaration ownership and result replay work across cached ancestors. They
  cannot recompute an arbitrary expression previously evaluated by a caller;
  invalidate the function owning that computation when its input changes.
* Live Python function-code/signature replacement invalidates the corresponding
  retained views while preserving node/state identity. The demo now uses ordinary
  module-level functions; end-to-end automatic source-watcher integration for
  the new decorator is not established. Native engine reload
  and native-state migration remain outside this throwaway experiment.

## Verification

Headless behavior and the original prototype:

```sh
.venv/bin/pytest -q tests/test_gui_window_prototype.py tests/test_retained_gui_prototype.py tests/test_gui_prototype.py tests/test_gui_prototype_rendering.py
```

Real GPU checks (use a reserved agent desktop/session):

```sh
.venv/bin/pytest -q tests/test_retained_gui_gpu.py
.venv/bin/pytest -q tests/test_retained_gui_layout.py
```

GPU tests read pixels back to verify clipping, alpha, child-to-parent composition
without ancestor execution, stable texture references, zero clean-frame captures,
GL state restoration and deletion of withdrawn window textures. These readbacks
are verification only; the runtime does not read textures back to the CPU.

Layout checks compare incremental GPU images against fresh full renders through
growth, shrinkage, zero/fractional extents, fixed clipping, vertical/horizontal
flow and 80 seeded transitions. Other checks cover wrapped text, stable sibling
IDs/textures, keyed reorder/insert/remove, shared-model invalidation, returned
edits plus resize, min/max bounds, portal isolation, hit-region reflow, host
cursor placement, exception retry, an 80-level chain and feedback diagnostics.
Additional regressions cover new width constraints while a returned edit is
pending and final-extent backgrounds with nested translucent composition.
