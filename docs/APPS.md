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
from meltygui.editor.live_view_views import draw_function_live
from meltygui.graphics import Filter
from meltygui.chat import draw_chat_interface, register_chat_backend
```

`meltygui.persisted(name, factory, app_id=...)` retains app-owned objects in
addition to automatically retained view state. Keep an existing app's `app_id`
when migrating so its session file remains the same.

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
