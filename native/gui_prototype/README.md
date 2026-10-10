# Rust @gui experiment

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the complete architecture,
supported behavior, gaps versus core rendering, development history and findings.
See [COLLISIONS.md](COLLISIONS.md) for the new retained column/row and window
collision experiment. Run `.venv/bin/python examples/gui_collision_lab.py`.

Throwaway prototype, separate from `core_render.render_func`. The existing tile
manager hosts the comparison; the measured collection subtree uses either the
production Python wrapper or a dynamically shaped Rust runtime.

## Run

From the meltygui checkout:

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python examples/tile_manager.py --rust-gui
```

The ordinary tile-manager example is unchanged without `--rust-gui`. The
experiment has its own app ID (`meltygui-rust-prototype`) and saved session.
The build is opt-in, uses the invoking Python interpreter, and does not alter
the normal package dependencies. Rust 1.77.2 and Python 3.12 were tested.
`MELTY_RUST_TOOLCHAIN` can select another installed rustup toolchain.

Choose 25, 100, 300, or 1000 rows. Drag the integer/float fields, click collection
headers to collapse, scroll each pane, or resize the shared tile divider.
Edits on the two sides are independent. Reset values restores matching data.
Pause either panel to try the other in isolation; in particular, pause Python
to feel the native controls' responsiveness without the Python subtree limiting FPS.

The baseline button switches between:

- **Same bodies**: identical Python implementations of `flat_button`,
  `draw_collection`, `draw_int`, and `draw_float`, under different decorators.
  Both body-call counts are shown. Compare the same expanded layout on both
  sides. This measures the cost of the two hosts, including their unequal
  feature sets, rather than differences in the widget bodies.
- **Existing collection**: the actual production `draw_collection`, including
  its existing numeric controls. Nested collections explicitly use its
  `render_func` host, not `fast_draw_collection`. This is a practical comparison,
  not an isolated wrapper benchmark.

The app continuously redraws and deliberately disables tile replay for these
panels. It shows rolling median/p95 collection CPU wall time over 120 samples;
GPU time and the surrounding tile/window chrome are excluded. Both panels
contribute to the same app frame, so the slower one still limits overall FPS.
Cold/new rows and user interactions can temporarily influence the rolling window.

## Implementation

The application uses `@os_window` and ordinary `@gui` functions. The window
owns the native state, input dispatch, frame boundaries and cleanup:

```python
from meltygui import gui, os_window

@gui(inject={"selection": list})
def my_view(input_value: object, draw_state=None, selection=None,
            completely_custom_argument=12, **extra_arguments):
    # This field requires no declaration or native-code changes.
    draw_state.another_custom_field = completely_custom_argument
    return False, input_value

@os_window(name="Native experiment", width=600, height=400)
def app():
    my_view(completely_custom_argument=20, caller_only_argument="hello")
```

`os_window` is `gui(glfw_window=True, ...)`, using the existing GLFW/Wayland
window host. `input_value` is optional in GUI function definitions. Drawing-only
functions may return nothing; this preserves their caller's input as unchanged.
Explicit editable `(changed, value)` returns keep the same contract. Normal
`@gui` caching defaults also apply to `@os_window`; use `use_cache=False` for an
immediate root or `live=True` when it needs continuous host frames.

Ordinary ImGui drawing and widgets work inside cached views:

```python
from meltygui import os_window
import imgui

@os_window(name="example main", width=1400, height=1000)
def example_app():
    imgui.text("hello")
```

Import the prototype decorator before `import imgui` so both names use Melty's
native binding. `from meltygui import imgui` also works. Run
`.venv/bin/python examples/gui_imgui_widgets.py` for button, checkbox, slider and
text-entry examples. Pointer input invalidates the hit cached view; keyboard and
characters go to its focused context. Active editing requests frames for the
caret and key repeat. Clean unrelated views keep their textures. The granularity
is a whole view rectangle, including text-only areas, not individual widgets.
That invalidation boundary does not claim the native window's gestures: empty
space still moves the window, and unclaimed right/double-right drags resize it.
ImGui state stays in each retained context; application values still need local
injected state or the normal `(changed, value)` return contract.

Focus traversal across cached contexts, IME, cross-context drag/drop, and popups
extending outside a view's texture remain unsupported. This is an input adapter
for ImGui, not a production focus-system replacement.

Rust owns the field-name table and slot storage for every field, the view
registry, scoped parent/child IDs, keyword resolution, owned state injection,
basic width/height measurement, ImGui group/ID scopes, event injection and
exclusive wrapper/body timing. There is no fixed DrawState field struct or
separate slow dictionary for custom fields. Exact Python bool/int/float values
use native representations when possible; large integers, subclasses and other
Python objects retain Python references and identity.

The Python decorator inspects the signature once. It recognizes typed
`DictConversion` parameters and supports additional factories via `inject=`.
Caller overrides are transient; direct writes to native state preserve edited
parameter values beneath explicit caller overrides. Python function-code
replacement refreshes the plan without changing native node IDs or owned state.
Body and injected-state factory callbacks release runtime borrows before
executing Python, allowing nested render calls.

Native identities are scoped to their owning window/view and stable keys.
Closing the OS surface releases its native state and cached GPU resources before
the renderer/context is destroyed. The app does not register a cleanup hook.
The original wrapper-only benchmark directly exercises the low-level native
call ABI so its measurements remain independent of the window/cache adapter.

## Second experiment: retained command replay

The new `--rust-cache` mode explores independent invalidation, returned-value
replay, texture composition and owned internal windows. See [RETAINED.md](RETAINED.md)
for its architecture, controls and intentional limits. It is separate from the
original `--rust-gui` comparison described above.

## Original immediate-mode experiment: deliberate limits

- This is an uncached immediate-mode subset, not feature parity with the full
  Python wrapper. Codecs, conversion chains, undo, searches, drag/drop collection
  reordering, parameter-link invalidation and native-state session persistence
  are not implemented in the new engine.
- Windowing, scrolling, clipping at the tile boundary, shared styling, and the
  event router still belong to the surrounding Melty host. Native views live
  inside a normal `render_func` panel; arbitrary alternating old/new trees have
  not been implemented.
- Drawing uses the existing ImGui Python extension. No direct Rust/C++ ImGui
  bridge, extra GPU renderer, or render-thread parallelism is introduced.
- Python body/signature replacement is tested. Compiled-engine reload and
  native state migration across engine versions are not implemented; rebuilding
  the extension requires a new test-app process. Source-file watcher integration
  for the locally generated demo functions is not claimed.
- IDs are runtime-local; field layouts share an intern table and can consume
  extra storage when many unrelated field names are introduced. This first
  implementation does not provide JIT-specialized field access or a fixed-field
  comparison, so it cannot yet quantify the precise cost of dynamic storage.
- Normal named parameters and `**kwargs` are supported. Positional-only
  parameters and `*args` deliberately fail at decoration time.

## Verification and measurement

```sh
.venv/bin/pytest -q tests/test_gui_prototype.py tests/test_gui_prototype_rendering.py
.venv/bin/python tests/benchmark_gui_prototype.py
```

The headless benchmark alternates host order, warms both paths, checks identical
body counts and mutable input identity, and subtracts nested calls from native
body timing. It uses the existing wrapper profiler's Melty harness and does not
validate GPU or window behavior. The UI comparison is the separate live check.

Measurements with Rust 1.77.2, 30 measured samples after 10 warmup frames:

| Rows | Body calls | Python collection | Rust collection | Ratio |
| --- | --- | --- | --- | --- |
| 25 | 102 | 21.15 ms | 0.73 ms | 29.0x |
| 100 | 402 | 85.74 ms | 2.88 ms | 29.8x |
| 300 | 1202 | 262.03 ms | 8.65 ms | 30.3x |

These numbers compare a production host against a smaller experimental host.
They establish headroom for the prototype, not the speedup of a future complete
port. Live-window measurements differ with styling, input routing, and scheduling.
