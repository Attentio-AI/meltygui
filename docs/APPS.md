# Building an app

Install the framework into the same environment that runs the app. Both the
package name and import name are `meltygui`. There is no `src` package and no
requirement to launch from the framework checkout.

When contributing reusable features to the library, also follow the
[ownership and render-function contracts](../CONTRIBUTING.md).

```python
import meltygui
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion

class EditorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.text = "Hello"

@meltygui.glfw_window(name="Editor", app_id="my-editor", width=900, height=700)
@render_func(use_cache=False)
def editor(input_value=None, state: EditorState = None):
    changed, state.text = meltygui.draw_text(state.text)
    return changed, input_value
```

The loop starts after the main module finishes defining its windows.
Injected state persists across frames and sessions. Call render functions every
frame, returning `(changed, value)`. For nested windows, pass `closable=True` and
an `open_requested` event to the same call every frame. Use `glfw_window=True` to
host a child in a native window; its lifecycle follows the same rules.

## Common imports

```python
from meltygui import draw_text, draw_voxels, glfw_window, imgui
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import defaults, no_save
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.render_host import RenderHost
from meltygui.view.code_view import draw_function_live
from meltygui.graphics import Filter
from meltygui.chat import draw_chat_interface, register_chat_backend
```

`meltygui.persisted(name, factory, app_id=...)` retains app-owned objects in
addition to automatically retained view state. Keep an existing app's `app_id`
when migrating so its session file remains the same.

## Tiled editors

Mark an editor with `@render_func(multi_instance=True)` to offer it in every
tile's editor dropdown. Registration happens when its module is imported; the
flag does not open a window. A `Tile` in the app model owns the selected function
reference and its `input_value`. `Tile` and `Split` are `DictConversion` models,
so the normal app/session persistence saves the layout and function references.

```python
from meltygui.model.tile_model import Split, Tile

@render_func(multi_instance=True, tint=(0.2, 0.4, 0.6))
def draw_scene(input_value: Scene, state: ViewportState = None):
    ...
    return changed, input_value

# Store this on your app model. A layout always has a Split root.
app_model.tiles = Split("x", [
    Tile(render_func=draw_scene, input_value=scene),
    Tile(render_func=draw_scene, input_value=scene),
])
```

The host declares `multi_instance_renderers=()` to receive the eligible
function references from core, and passes that parameter to `draw_tiles` along
with its injected `TileManagerState`. The selected editor receives the tile's
input unchanged: the app supplies a compatible value, or the editor accepts
`None` and uses injected model/state. Choosing another editor does not construct
or convert model data.

Each tile has independent injected view state. Switching editors and switching
back restores that tile's editor state. Splitting inherits the renderer and
shares the input value, while creating a new view instance. See
`examples/tile_manager.py` for a runnable demo with counters and notes.

Hosted renderers can declare `instance` and `layout_frame` parameters. These
receive the tile's existing conversion identity and its four shared frame edges,
so instance-targeted commands and nested column layouts stay within that tile.
An editor created by a corner split first renders when the drag ends, after its
initial size is known.

Files in an installed library are read-only to the live editor. App source roots
are registered from the app's entry point and decorated functions. An editable
framework checkout can also be edited; an ordinary wheel in site-packages cannot.

## Native binding imports

MeltyGUI uses a namespaced ImGui binding. Low-level app code should use
`from meltygui import imgui` (or `import meltygui_imgui as imgui`). Mixing contexts from
upstream `imgui` with MeltyGUI's binding is unsupported. The upstream packages
can coexist because the support wheels install different Python namespaces.

The optional tensor backend similarly uses `meltygui_pycuda`. A child project's
own `pycuda` dependency remains independent of MeltyGUI's internal backend.
