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

### Window size and position: `initial=`, never `width=` / `height=`

`width=`, `height=` and `window_pos=` on a call are applied **every frame**: they
pin the window, so the user cannot resize or move it and the persisted geometry
is thrown away. To give a window a starting size or place, pass
`initial={...}`; it is written once, on the window's first frame, and the user's
(persisted) geometry wins afterwards:

```python
draw_dep_manager(value, name="Dependencies", closable=True, as_window=True,
                 open_requested=clicked,
                 initial={"width": 760, "height": 620, "window_pos": (240, 90)})
```

`initial` also works at decorator level (`window(initial={...})`); a caller's
`initial` wins over the decorator's. Pass `width=` / `height=` only to views laid
out by their parent (a tile, a cell, a button), where the parent owns the size.

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

## Settings

`@glfw_window(settings=...)` takes a plain dict of defaults and keeps it between
runs. The dict is loaded in place before the loop starts, so the app reads it
like any dict; nested dicts are sub-folders of the settings window.

```python
SETTINGS = {'font_size': 14, 'editor': {'tab_width': 4, 'wrap': False}}

@meltygui.glfw_window(name="Editor", app_id="my-editor", settings=SETTINGS)
def editor():
    size = SETTINGS['font_size']
```

The dict's own values are the schema: a saved value only lands on a key the
dict has, with the same type, so a renamed or removed setting disappears and a
new one shows its default. The file is `$XDG_CONFIG_HOME/<app_id>/settings.json`
(default `~/.config`), one section per window name, written when the settings
window changes a value and on exit. A window with settings shows a cog in its
title bar, just inside the window controls, that opens the settings window
(the dict drawn as a child native window). See `meltygui/examples/settings_demo.py`.

## Tiled editors

Mark an editor with `@render_func(multi_instance=True)` to offer it in every
tile's editor dropdown. Registration happens when its module is imported; the
flag does not open a window. A render function defined elsewhere (a library
view) registers with `multi_instance(view)`, and keeps its own `display_name` /
`icon` / `tint` in the picker. A plain callable (no render boundary of its own)
is offered through the host's `multi_instance_renderers`:

```python
from meltygui import draw_claude_chat     # one call of draw_chat_interface
draw_tiles(tiles, draw_state, multi_instance_renderers=(draw_claude_chat,))
```
 A tile calls its editor with `layout_frame` (its
four edge dicts); an editor that lays out columns forwards it to the view that
does, as `draw_chat_interface(layout_frame=...)` does, so the columns stay
inside the tile instead of adopting the window frame. A `Tile` in the app model owns the selected function
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

## Designing a feature

**Locality.** Each thing lives next to what it belongs to, at the smallest scope that holds
all of its users. Apply it to code, state, UI and lifetime, and let structure grow only when a
second user appears somewhere else:

- **Code**: a feature is one file in the app that uses it: its state class, its handful of
  functions and its view together. Move a part into meltygui / meltygui_pro when a second app or
  view needs it, or the package-split rules require it, and say what asked for it.
- **State and config**: state lives on the thing it describes. View state is one injected
  `DictConversion` per view (`@no_save` for process-lifetime fields); config a user edits lives
  in the project it configures (`[tool.melty.<feature>]` in `pyproject.toml`), read the way the
  app already reads it, in the flattest form that works; machine-local state lives in the
  injected state or the file-meta store. Prefer plain data (`dict`, tuples, strings); add a class,
  registry or service when two things must share it, not because a feature might grow.
- **UI**: a feature appears beside the things it acts on and takes its context (project, file,
  selection) from those neighbours, the way the app's most recent features do. It is built from
  the widgets already here; menus and chords go in the app's entry module; both backends
  identical.
- **Lifetime**: what a feature starts (a process, a thread, a watch) belongs to it: stopped
  by its own explicit action and at app exit, never as a side effect of something unrelated.
  Background work is a daemon thread writing plain fields and calling `request_render()`, the
  view invalidating while it runs; heavier machinery only when that pattern cannot do it.

Older subsystems grew under different pressures; take proven recipes from them (spawning,
streaming, waking the UI), not a new feature's shape.

**A design doc is a page**: the state fields, the function signatures, the view, the build order,
and a "later" list. The first version ships the core; extras wait in "later" until someone wants
them. When a design genuinely needs more than this, say why in a sentence and go ahead.

## Native binding imports

MeltyGUI uses a namespaced ImGui binding. Low-level app code should use
`from meltygui import imgui` (or `import meltygui_imgui as imgui`). Mixing contexts from
upstream `imgui` with MeltyGUI's binding is unsupported. The upstream packages
can coexist because the support wheels install different Python namespaces.

The optional tensor backend similarly uses `meltygui_pycuda`. A child project's
own `pycuda` dependency remains independent of MeltyGUI's internal backend.
