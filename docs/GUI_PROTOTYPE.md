# Rust `@gui` prototype: design, use and implementation notes

This records the prototype developed on 2026-11-06/07. It is an opt-in experiment
alongside `@render_func`, not a completed replacement. The priority is a small,
fast runtime with plain Python view functions and reusable state injection.

The current implementation has stable identity, native draw-state persistence,
relative geometry, cached nested scrolling, `left`/`top` positioning, runtime
cost calibration and automatic raster selection.
[Geometry, scrolling and benchmark details](GUI_SCROLL_PROTOTYPE.md) supplement
this overview. [Matched plain-Python overhead measurements](GUI_OVERHEAD_BENCHMARK.md)
separate uncached dispatch, full retained redraw, unchanged frames and culling.
[Runtime calibration](GUI_CALIBRATION.md) describes local cost snapshots, passive
replay observations, opportunistic idle probes and their coverage limits.
[Automatic cache selection](GUI_AUTO_CACHE.md) records the current implementation,
code changes, matched default/calibrated results and remaining redraw overhead.
The [native editor](NATIVE_EDITOR.md) (2026-11-11) is the first large view built
on it; the event kinds, pointer shapes and the wake from native threads
described below were added for it.

## Short how-to guide

The retained capture path requires `meltygui-imgui` 2.0.5, which adds
`DrawCapture` with its fused view scope. Until that binding is published to the package index, install
its locally built wheel first. Use the interpreter that will run your app
for both installs (an editable checkout does not update dependencies when
its source requirements change):

```sh
.venv/bin/python -m pip install '/path/to/meltygui_imgui-2.0.5-<python>-<abi>-<platform>.whl'
.venv/bin/python -m pip install -e .
```

Then build the Rust extension and run an example:

```sh
.venv/bin/python tools/build_gui_prototype.py
.venv/bin/python examples/gui_scroll_lab.py
```

The build needs Rust/Cargo and produces the optional native extension. Prefer
the namespaced `imgui` import below; importing upstream `imgui` first creates an
incompatible native runtime.

### A complete small app

```python
from meltygui import gui, os_window, imgui


@gui(width=260, height=65)
def counter(input_value: str, count=0, draw_state=None):
    imgui.text(input_value)
    if imgui.button('Increment'):
        draw_state.count = count + 1
    imgui.text(f'Count: {draw_state.count}')
    return False, input_value


@os_window(name='Counter prototype', app_id='counter-prototype',
           width=620, height=300)
def app():
    counter('First counter', name='first', left=5, top=10)
    counter('Second counter', name='second', left=300, top=10)
```

`@os_window` provides the existing native-window/app lifecycle. `@gui` defaults
to retained caching. A view can return `(changed, value)` or return `None` for an
unchanged input. Local view-state edits do not by themselves mean the input model
changed. Keep mutable input identity when editing the model.

Use `use_cache=False` for an immediate host/toolbar body. `live=True` requests
more host frames; it does not force every cached descendant body to execute.

### A view shows what its caller passes

Every view behaves as an immediate mode widget, whether it is drawn directly
(`use_cache=False`), retained, or retained in a Melty window:

```python
changed, model.name = field(model.name)   # the edit is kept: the caller passes it back
field('fixed')                            # edits show for a few frames, then 'fixed' again
```

A retained view does not run every frame, so the host holds the edit it
returned as its live input while the caller has not answered: typing is never
lost to a caller that is a frame behind. A caller that still passes its old
value `SETTLE` frames later (3, in
[retained.rs](../native/gui_prototype/src/retained.rs)) is run once more and
what it passes then is shown. A caller that keeps the edit in a model passes the
model's value at that point, so nothing is reverted; one that passes a new value
of its own is followed at once. Small differences in timing between the hosts
are expected, the value a few frames later is not: see
[test_gui_immediate.py](../tests/test_gui_immediate.py),
[test_gui_immediate_view.py](../tests/test_gui_immediate_view.py) and the
bridge cases in [test_gui_legacy_bridge.py](../tests/test_gui_legacy_bridge.py).
A value edited in place (a dict, a `Source`) is the caller's own object and has
nothing to settle.

### Identity and repeated calls

```python
counter('Width', name='width')
counter('Height', name='height')
counter(dynamic_label, key='stable-counter', name=dynamic_label)

for row_key, value in rows.items():
    row_view(value, key=row_key)
```

An explicit `key` wins over `name`. A nonempty name wins over automatic
occurrence. Repeated unnamed/unkeyed calls work, but skipping an earlier
anonymous call to the same renderer shifts later anonymous instances. Use keys
or names around conditionals and keys for reorderable collections.

### Position, initial values and state

```python
counter('Width', name='width', left=5)  # Other axis uses its flow/default placement.
counter('Height', name='height', left=5, top=90)

counter('Floating', name='floating', melty_window=True,
        initial={'left': 80, 'top': 120, 'width': 300, 'height': 100})
```

`left` and `top` are relative to the enclosing retained content space, not
additional offsets from the layout cursor. They resolve independently. An axis
without a supplied, authored, initial, decorator or signature value follows
normal flow. Numeric signature defaults such as `top=0` are actual defaults,
not a request for automatic flow. Rows/columns own their cell allocations.

Use `initial` to seed a position or size once. Explicit call arguments override
state when the call is resolved. Subsequent state edits use the same names:

```python
draw_state.left = 40
draw_state.top = 80
draw_state.scroll_offset = (0, 200)
```

Cached bodies draw in their own local coordinate space. The framework places
their output using `left`/`top`; do not add the placement again to every drawing
primitive inside the body.

`window_pos` is no longer a live prototype API. Old saved tuples translate into
the scalar fields on load. This did not rename the legacy `@render_func` API or
OS-backend geometry structures.

### Persistent and transient state

Declare feature state in the signature, or inject a state object:

```python
from meltygui.core.conversion.dict_conversion import DictConversion


class Selection(DictConversion):
    def __init__(self):
        super().__init__()
        self.items = []


@gui(no_save=('preview',))
def inspector(input_value, preview=None, selection: Selection = None,
              draw_state=None):
    # selection is owned by this view instance; it is not shared by other views.
    imgui.text(f'{len(selection.items)} selected')
```

Named parameters are mirrored on state when resolved. Arbitrary caller kwargs
do not automatically become state fields. `DictConversion` annotations with an
absent/None default are injected; explicit `inject={'field': factory}` is also
available. Caller-provided instances are borrowed rather than replacing the
factory-owned instance. Existing serializer rules still govern the values.

Use `no_save` for runtime-only parameters, callbacks or handles. Geometry is not
globally excluded: `no_save=('left',)` excludes one coordinate, while
`no_save=('left', 'top')` excludes both. Framework context, events and derived
measurements have their own exclusions.

### Scrolling

```python
@gui(width=500, height=400, scroll=True)
def list_view(input_value: dict):
    for index, (row_key, value) in enumerate(input_value.items()):
        row_view(value, key=row_key, left=0, top=index * 28)
```

Here `row_view` is another `@gui` function with an appropriate row height.
Children stay in unscrolled content coordinates. Wheel input routes through
nested viewports; scrolling changes composition and visibility. Raw ImGui drawing
in the viewport body is fixed chrome, not automatically virtualized content.

For in-place external model changes, use the associated cache's
`cache.invalidate(model)` instead of expecting the framework to hash the model.
The low-level harness consumes queued state changes with `cache.flush()`; the
app host drives that lifecycle during frames. Mutation and GL work belong on the
owning render thread.

### Input events and pointer shape

A view that names events with `listen=` is event driven: raw pointer and key
samples never rerun it, and what it listed arrives as lists in `view_events`.

```python
@gui(listen=('keys', 'text', 'pointer', 'wheel'), cursor=mouse_cursor.TEXT)
def field(input_value: str, view_events=None):
    for key, mods in view_events.get('keys', ()):
        ...
    for event in view_events.get('pointer', ()):   # Pointer(kind, x, y, clicks, ctrl, shift, alt, button)
        ...
```

| Name | Arrives as |
| --- | --- |
| `keys` | `(key, mods)` presses and repeats, for the keyboard owner |
| `text` | typed strings, for the keyboard owner |
| `focus` | True / False when the view gains / loses the keyboard |
| `pointer` | `Pointer` press, drag and release of any button (`button`: 0 left, 1 right, 2 middle), in the view's coordinates |
| `hold` | a `Pointer` of kind `hold` once a frame while a button is held and the pointer rests outside the view: what a drag past an edge needs to keep scrolling |
| `hover` | a `Pointer` of kind `hover` as the pointer moves over the view with no button held, and one of kind `leave` |
| `touch` | `Touch(kind, contact, x, y)` contacts, from hosts that report them through `ImGuiInput.touch` |
| `wheel` | `(horizontal, vertical)` steps no viewport consumed |
| `scroll` | True after a viewport under the pointer scrolled |

Window gestures are the host's by default over every view: a left drag moves
the Melty or OS window, a right drag resizes it
([rules](WINDOW_COLLISION_COLUMNS.md#gestures)). Listening for `pointer` is
what overrides the left one (`ImGuiInput.left`: text selection, a list's
rows), inside the view's own rectangle, so a Melty window's title strip still
moves it. A view that uses only part of its rectangle says so each run with
`cache.imgui_input.reach(node, (x0, y0, x1, y1))`: `draw_editor` takes the
pointer over its rows, and the empty space below the last one moves the
window. Nothing a view listens for overrides the right one; right-button
`Pointer` events still arrive (a click is the view's to use), the drag also
resizes. An ImGui item keeps the button that activated it.

A left press gives the keyboard, any press the pointer, to the innermost
listening view around the hit one, so a popover or an inline widget leaves the
keyboard with its editor. Who has the keyboard is `cache.focus.keyboard`
([focus.py](../meltygui/core/input/focus.py)); `cache.focus.take(node)` moves
it. A view that lists nothing (`listen=()`) costs one
dictionary truth test a frame. `cursor=shape` (a `core/input/mouse_cursor.py`
shape) declares the pointer's shape over the view without running a body: the
innermost declaring view around the hovered one wins, and the shape a press
began with is kept until every button is up. The contract is the module text of
[gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py).

### Temporary popups

A dropdown list, a colour picker or a menu is an ordinary view called with
`popup=<open event>`. The call is made on every run of its caller, like any
other; the popup is shown from the run in which the event is true until
something closes it. Nobody keeps an open flag:

```python
@gui(width=36, height=22)
def swatch(input_value, width=0., height=0.):
    """A colour chip: a click opens the picker under it."""
    pressed = imgui.invisible_button('##swatch', width, height)
    imgui.get_window_draw_list().add_rect_filled(0, 0, width, height, pack_color(*input_value[:3]), 4.)
    return picker(input_value, name='picker', popup=pressed, left=0., top=height + 2)
```

`picker` is any `@gui` view (it does not declare `popup`, and can be placed
inline as well). `left` / `top` are in the caller's coordinates, as for every
child. The caller sees `(changed, value)` as from any view.
[examples/gui_popups.py](../examples/gui_popups.py) has this picker and a
dropdown; the editor's icon picker and colour swatches are popups too.

What the framework does, so that no view has to:

| | |
| --- | --- |
| Open | A truthy `popup=` opens it. Pass an event (a click, `is_item_activated()`), not a level that stays true. |
| Shown as | A Melty window without title, move or resize, above its caller's clip. It is kept on the surface: one hanging below (right of) its caller with no room there goes above (left of) it by the same gap, then it is moved inside. |
| Press outside | Closes it, and the press still reaches what is under it (a caret is placed). A press inside keeps it. |
| Press on the anchor | Toggles: the press that closed a popup cannot open it again. |
| Escape | Closes the innermost open popup and is not delivered as a key. |
| Keyboard | A popup that listens for `keys` takes the keyboard when it opens. One that does not (sliders, a list the pointer picks from) leaves it where it is. |
| Close | The keyboard returns to the view that had it at the opening, if it is still inside the popup. The caller runs once more; its call no longer declares the popup. |
| Nesting | A popup opened from a popup is below it: a press in the child keeps the parent, a press in the parent closes the child, closing the parent closes both. |
| Each opening | Starts afresh: the popup's `no_save` parameters are their defaults again. Saved parameters persist. |
| Caller gone | A popup whose caller stops being declared closes with it. |

A popup closes itself after a pick with `cache.focus.close()`; what it
returns from that run still reaches its caller:

```python
@gui(listen=('keys', 'pointer', 'hover'), no_save=('index',))
def options(input_value, choices=(), index=None, width=0., height=0., draw_state=None, cache=None, view_events=None):
    if index is None:               # just opened: `index` is not saved
        index = choices.index(input_value) if input_value in choices else 0
    ...                             # arrows and hover move `index`, Enter or a press sets `picked`
    if picked:
        cache.focus.close()         # the keyboard returns to whoever had it
        return True, choices[index]
    return False, input_value


@gui(width=160, height=24)
def dropdown(input_value, choices=(), width=0., height=0.):
    pressed = imgui.button(str(input_value), width, height)
    return options(input_value, name='options', popup=pressed, choices=choices,
                   left=0., top=height + 2, width=width, height=len(choices) * ROW)
```

- Is it open (to highlight the anchor): add `return_extras=True`; the third
  value is the popup's state, None while it is closed.
- Name or key the call (`name='options'`), as for any call under a condition.
- A view that takes the keyboard by itself (`focus=True` on `draw_editor`)
  asks `cache.focus.within(node)` first: the keyboard is with the view or with
  a popup opened from it. Taking it back from one's own popup would close it.
- A popover whose being open is a model's (the editor's suggestions, its find
  bar) stays a caller-declared `melty_window=True, content_offset=(0., 0.)`
  view: the caller declares it while its model says so.

Cost: a closed popup has no node, no state and no body. Its call is the
declaration's identity lookup, about 0.84 us in each run of its caller and
nothing in a frame the caller does not run in. With no popup open and no
press just handled a frame pays two truth tests. Measurements:
[the 2026-11-12 pass](#focus-stack-and-temporary-popups-2026-11-12). The
rules are the module text of [focus.py](../meltygui/core/input/focus.py), the
tests [test_gui_focus.py](../tests/test_gui_focus.py).

### Default views: `draw_any`

`draw_any` draws a value with the view registered for it and is called like that view. A value
that is read by keys goes to `draw_collection`, which draws every item the same way; such a value
among them is a branch that a press opens.

What is read by keys is decided by type, in [keyed.py](../meltygui/core/rendering/keyed.py): a dict
and anything with `keys`, `__getitem__` and `__len__` (a mapping proxy, `UserDict`, `ChainMap`, an
h5py group, a DataFrame), a list, a tuple and a deque by index, a dataclass and a named tuple by
their fields, a `SimpleNamespace` and a `DictConversion` by their attributes. A type's `Keyed` is
four functions (`keys`, `get`, `put`, `count`) and is all `draw_collection` knows of it; without a
`put` (a tuple, a mapping proxy, a frozen dataclass) its values are read only. Another type is one
registration, by type or by name:

```python
from meltygui.rust import keyed_as

keyed_as('Module', keys=lambda module: [name for name, _ in module.named_children()], get=getattr)
```

A view registered for the type, a shape or the name still wins over `draw_collection`.

```python
from meltygui.rust import Shaped, draw_any, is_default_for

@is_default_for('tint', Shaped(tuple, (3,), float))     # a name and a shape; a type works the same
@gui(listen=())
def swatch(input_value: tuple, width=0., height=0.):
    ...

changed, settings = draw_any(settings, name='settings', expand=2, left=8, top=40, width=420, height=600)
```

The public names are in `meltygui.rust` ([rust.py](../meltygui/rust.py)): `draw_any`,
`is_default_for`, `Shaped`, `Keyed`, `keyed_as`, `draw_collection` and the value views. Importing it loads nothing; a
name is loaded from its module when it is first asked for.

`@is_default_for` goes above `@gui` and takes types, names and `Shaped` patterns in any mix. The
most specific registration wins: the name the value is drawn under (`name=`, or its key in a dict),
then its shape, then its type and base classes, then `Keyed` (every type that is read by keys),
then `object`. A string also matches a type by its `__name__`, for
types the registering module should not import. A later registration replaces an earlier one, so
an app overrides a built in view. The built in ones are `draw_bool`, `draw_int`, `draw_float`,
`draw_str`, `draw_none` and `draw_value` (a bounded `repr` for the rest) in
[value_view.py](../meltygui/view/value_view.py), and `draw_collection` in
[dict_view.py](../meltygui/view/dict_view.py). Each module's docstring says how to change it.

Cost (`tests/benchmark_gui_any.py`, hidden window, no swap, 2026-11-12): routing a value takes about
0.1 us. A dict's frame does not depend on its size, 1,000 against 1,000,000 entries: idle 0.2 ms,
a wheel step 0.5 ms, a fling of 30 rows a frame 1.1 ms, dragging a number 0.4 ms, a typed character
0.4 to 0.5 ms, opening a branch 1.2 ms. The frame a dict is first shown in lists its keys: 1.5 ms at
1,000 entries, 2.5 ms at 100,000, 15 to 18 ms at 1,000,000.

Not there yet: rows of different heights, sets, add / delete / reorder,
search, an enum or colour view, undo of an edit. `meltygui.draw_any` is still the legacy one.
Every row keeps its view state once it was shown, about 1.5 to 3 KB a row (see the known issues).

### Results from other threads

A view whose background result arrived asks for another run. The injected
`cache` offers two ways, and nothing else of it is safe off the render thread:

```python
node = cache.current
wake = lambda: cache.wake(node)        # for a Python thread
handle, token = cache.waker(node)      # for a native thread that never takes the interpreter lock
```

`cache.waker` returns a C handle and a token. The native thread calls the handle
with the token; that queues the token and rouses the window system's event wait
(`window_api.poke`: GLFW's empty event, the native Wayland wake socket). The
frame loop turns queued tokens into `cache.wake(node)` in `app.run`,
`NativeApp.frame` and `cache.flush()`. The queue is `Wakes` in
[wake.rs](../native/gui_prototype/src/wake.rs); the Python side is `waker`,
`drain_wakes` and `close_wakes` in `core/windowing/glfw_utils.py`. A Python
worker that computes between frames must also give way to them:
`glfw_utils.park()` between pieces of well under a millisecond
([why](NATIVE_EDITOR.md#rules)).

A model that is read as a plain value (`errors=analysis.errors`) is read in
its caller's body, so it is the caller that has to run again when a worker
swaps the value. `Readers`
([gui_readers.py](../meltygui/core/rendering/gui_readers.py)) does that for
any model: `readers.note()` in the getter records the view whose body is
running, `readers.wake()` (thread safe) runs every recorded view again. A
host that runs its root on every frame has nothing recorded and gets a frame
request, as before. Without it a cached root keeps passing the value it read
on its last run ([test_gui_readers.py](../tests/test_gui_readers.py)).

## Design decisions

### Stable identity without inspecting the input

Identity is hierarchical: app/window scope, logical parent, renderer, and local
discriminator. It never examines `input_value`, its type, contents, address or
hash. Explicitly supplying an application key is different from the framework
discovering identity from the input.

The renderer defaults to `__module__` plus `__qualname__`. `state_id='...'`
provides an explicit renderer identity for intentional renames or moves. Choose
distinct IDs for distinct renderers.

Discriminators have separate namespaces:

1. Explicit key, including `key=None`.
2. Nonempty name.
3. Occurrence among unnamed/unkeyed calls to this renderer in the parent execution.

Named/keyed calls do not consume occurrence counters. Counters reset per parent
execution and unwind on failure. Independent cached redraws reuse their resolved
identity rather than consuming another occurrence. Duplicate explicit identities
within a retained owner are errors.

Supported keys are exact builtin strings, signed 128-bit integers, None and
tuples of those types. Arbitrary object hashing is not used.

Stable IDs are deterministic 128-bit values: a versioned typed encoding hashed
with SHA-256, truncated to 16 bytes. Hashing happens on identity-cache misses.
There is no collision-detection layer, by design. Small runtime handles are used
for graph operations; they are not persistent identifiers.

### One state object, a small fixed core

The fixed core is `unique`, `dlt_count`, and the geometry/context fields needed
by the renderer. Feature-specific fields come from each view's parameters and
injected state, rather than expanding one universal class.

Native state distinguishes resolved values, authored overrides and factory-owned
objects internally. This preserves caller/default precedence and ownership; it
is not a second persistence store. Generally, resolution is:

```text
explicit caller argument > authored/initial state > decorator default > signature default
```

Current geometry and context are injected by the framework as needed. The
serializer writes the actual native state object by field name and reconstructs
native slots on load. Slot numbers, graph handles and GPU resources are not saved.

### Session lifetime differs from capture lifetime

The existing `AppSession.draw_state_registry` and save/load pipeline own
persistence. There is no parallel archive of retired-view snapshots.

| Event | State objects | Rendering resources |
| --- | --- | --- |
| Ordinary cached frame | Reuse | Replay |
| Hide a view | Keep the same objects in the session | Retire draw captures |
| Evict an offscreen capture | Keep state and declarations | Release draw buffers |
| Show again | Reuse objects and parameter values | Recreate as needed |
| Checkpoint | Determine save eligibility without deleting live state | Remain usable |
| Close and reopen app | Restore eligible serialized state; prune unused records | Rebuild |

Save eligibility follows the declared/reachable view tree, not whether a cached
body happened to execute or whether every child is currently visible in pixels.
The existing `dlt_count` convention is retained: used state is refreshed, the
serialized countdown is decremented, and expired objects are dropped. Loaded but
unused state therefore does not accumulate across launches.

### Geometry is relative state, absolute positions are queries

The graph reads `left`, `top`, width and height from state slots. Scroll offsets
transform child content space rather than changing child positions. Content
offsets represent origins such as the area below window chrome.

Coordinate conversion is available mid-frame without drawing view bodies:

```python
cache.position(node)
cache.position(node, relative_to=parent, content=True)
cache.graph.convert_point((x, y), source=node, target=other,
                          source_content=True, target_content=True)
```

These use graph-local node handles obtained from `cache.current`, not
`draw_state.unique`. Cached absolute queries use a geometry-root revision. Cache
hits do not validate an ancestor chain. Rendering and hit testing primarily use
local coordinates, so scrolling does not demand absolute positions for the whole
subtree. The current prototype handles translations within one OS surface.

### Cache work follows changes and visibility

Actual state writes enqueue affected work. Position changes update placement;
scroll changes update viewport composition; size/content changes can rerender
the affected bodies. Unchanged primitive assignments do not invalidate work.

A local spatial index selects children for each viewport. Missing captures render
on demand; resident content replays. With `Toggles.Rust.auto_cache = True`,
cheap descendants share a pass and costly reusable subtrees can acquire raster
storage. Host roots and portals remain presentation boundaries. See
[automatic raster selection](GUI_AUTO_CACHE.md) for policy and measurements. Offscreen capture subtrees have a soft
256-tile/32-MiB budget, with visible and active resources protected. Eviction does
not discard state. Pure wheel gestures defer hover settling until scrolling stops.

## Implementation lessons and superseded approaches

| Problem encountered | Decision or correction |
| --- | --- |
| Native state survived frames but not app reopening. | Connect the native objects to the existing session serializer and registry. |
| Runtime allocation IDs and `id(surface)` could not identify views across launches. | Use deterministic scoped IDs, with separate runtime handles. |
| A 32-bit ID needed collision handling. | Choose 128 bits and omit collision detection. |
| Repeated calls collided; occurrence-only identity shifted under conditionals. | Use key, then name, then anonymous occurrence. Call-site inspection was discussed but rejected for cost and source-edit fragility. |
| Cached decorators were forwarded as explicit arguments and masked state edits. | Preserve which keys actually came from the caller. |
| A proposed snapshot/archive system retained hidden views indefinitely on disk. | Reject it; persist objects with exclusions and the original pruning lifecycle. |
| Retirement confused releasing captures with destroying in-session state. | Keep state through hide/show and eviction; prune across persistence boundaries. |
| Window movement lived in a graph rectangle and was copied to state only at save time. Hiding could lose it first. | Remove that duplicate authority, `save_window_geometry`, and `move_window`; use state parameters. |
| Absolute-position derivation mixed parent relationships, scroll compensation and special cases. | Make coordinate spaces explicit and keep queries independent of rendering. |
| Scrolling could invalidate every descendant or scan the whole graph. | Use local indexes, dirty queues, local-space hit tests and cached composition. |
| A metadata read built every child's ID just to retrieve a flag. | Add scalar queries for hot paths; keep diagnostic `info()` out of them. |
| The host title bar scanned all native states looking for a legacy main-window owner. | Supply the mask owner explicitly for prototype surfaces. This was a major live-demo cost. |
| Older tests rounded logical extents and read textures using truncated sizes. | Preserve fractional geometry, round allocation sizes, and compare with fresh-render pixels. |
| Culling prevented a zero-height ordinary view from being remeasured. | Keep non-scroll dirty work eligible; apply viewport culling where appropriate. |
| Legacy constraint edges retained old screen-space projections after scrolling. | Rebase derived projections at operation boundaries and use visible owners for layout hit testing/painting. |
| `window_pos` was an awkward tuple-only API; `left=5` initially did nothing. | Use independent `left`/`top` throughout calls, state, defaults, initial values and exclusions. Migrate old saved names only. |
| Moving a cached child in a non-scrolling parent updated hit geometry but not its captured image placement. | Queue changed child placements and update composition without rerunning bodies. |

## Validation and performance evidence

The positioning milestone passed **273 focused tests**, including GPU
pixels, identity, persistence, geometry, scrolling, layouts, widgets and chrome.
Live Wayland checks exercised scrolling, floating-window dragging, hide/show,
restart persistence and independent positioning axes. These counts describe the
recorded runs, not a claim that every repository test or platform is covered.

The recorded RTX 4090 scrolling benchmark used 600-by-400 viewports, 20-pixel
rows and already captured ranges. With 10,000 rows, median CPU submission/GPU
times were approximately 48/10 us at depth 1, 122/59 us at depth 8 and 361/226 us
at depth 32. All measured warm scrolls executed zero view bodies. Candidate work
and resident texture bytes were independent of row count between 100 and 10,000.

Those measurements preceded the final `left`/`top` cleanup; they are historical
evidence for the scrolling design, not fresh measurements from this documentation
pass. See [the full conditions and reproduction command](GUI_SCROLL_PROTOTYPE.md#measurements).

### Performance work after the geometry prototype

| Step | Finding or implemented change |
| --- | --- |
| Raw-versus-`@gui` benchmark | Established the expensive all-visible redraw case: 0.288 ms raw versus 19.034 ms per-view retained rendering for the original trivial workload. |
| Detailed profiling | Located repeated bridge/argument work and per-view FBO/upload/resolve costs. Identity hashing alone was not the explanation. |
| Eviction eligibility | Stopped rescanning unchanged protected captures. A matched control changed from about 327 us to 96 us for the unchanged graphics frame, without raising budgets. |
| Lazy acknowledgement | Removed an unnecessary retained-result lookup on unchanged declarations; measured CPU redraw improvement was about 2%. |
| Runtime calibration | Added immutable cost snapshots, deferred profile loading, bounded asynchronous GPU samples and opportunistic idle probes. No calibration work blocks the first presentation. |
| Automatic raster selection | Made shared command replay the default for cheap descendants, with optional cost/reuse-driven textures and stable state/input ownership. |
| Redraw fast paths (2026-11-10) | Remembered declarations, a native executor, a fused ImGui view scope, in-place packet capture and lazy layout transactions. Full redraw went from about 33 us to 6 us per view, a fresh mount from 55 us to 20 us; see [the fast-path notes](GUI_REDRAW_FAST_PATHS.md). |

The latest matched all-visible rerun measures **20.732 ms** per-view raster,
**14.422 ms** automatic with defaults and **14.291 ms** automatic with calibrated
costs, versus roughly **0.30 ms raw**. It uses 341 logical views in every mode;
raster boundaries drop from 341 to one. The CPU gap is reduced, not solved.
Defaults and calibration make the same decisions in these measured workloads,
so their small timing difference is not evidence that calibration is faster.

The automatic-selection milestone passed **314 focused tests**. Two additional
native-settings tests fail with a GLFW/native-Wayland pointer mismatch in both
automatic and disabled modes; that broader run is not reported as fully green.
Opaque transition pixels are exact, while tested translucent cases differ by at
most two RGBA8 channel values. See [the complete findings](GUI_AUTO_CACHE.md#measured-results-2026-11-07)
for timing conditions, resource counts, profiles and failure controls.

### Focus stack and temporary popups (2026-11-12)

Before and after `popup=` and `cache.focus`, four interleaved rounds of each
tree, pinned to one core, CPU submission time on a hidden GL window: medians
of the rounds' medians in ms per frame (spread of the rounds in brackets):

| Benchmark | Row | Before | After |
| --- | --- | --- | --- |
| `benchmark_gui_redraw.py --trees 5:4,2:512 --hot --mounts 5 --frames 600` | 5:4 full redraw (341 views) | 2.200 [2.171 to 2.299] | 2.231 [2.180 to 2.318] |
| | 5:4 root only | 0.651 | 0.647 |
| | 5:4 unchanged frame | 0.317 | 0.318 |
| | 2:512 full redraw (513 views) | 3.067 [3.052 to 3.285] | 3.053 [2.998 to 3.293] |
| | 2:512 root only | 1.464 | 1.441 |
| | 2:512 unchanged frame | 0.321 | 0.321 |
| | mount 5:4 / 2:512 | 7.097 / 9.827 | 6.841 / 10.084 |
| `benchmark_editor.py --lines 1000,10000 --frames 400` | idle | 0.203 / 0.204 | 0.203 / 0.205 |
| (1,030 / 10,036 lines) | type | 1.286 / 1.301 | 1.281 / 1.305 |
| | caret | 1.699 / 1.546 | 1.712 / 1.558 |
| | enter | 1.498 / 1.409 | 1.514 / 1.413 |
| | scroll | 0.387 / 0.393 | 0.391 / 0.393 |
| | fresh | 0.913 / 0.933 | 0.954 / 0.940 |
| | busy, find, word, errors | | within 1% |

Every row is inside its rounds' spread and runs the same number of bodies a
frame. `fresh` (20 frames into rows never painted) is the one to read with
care: it varies by 10% between rounds of one tree, and measured 4% higher
over ten rounds at 1,030 lines. Run with and without the colour swatch's
popup call in one process, that call accounts for under 2% of it: a swatch
painted for the first time now also resolves its closed picker's identity,
6.3 us for the swatch body against 2.6 us. Afterwards a closed popup call is
0.84 us in each run of its caller. Views that call no popup take no new
branch on the remembered-declaration paths; the check sits where a call is
resolved in full.

## Known issues, limits and next work

- Found on 2026-11-12, present before the focus stack: after its host closed
  an editor session (`Editor`) stays in a reference cycle that holds three
  native states (`pane`, `caret`, `band`), so the cycle collector frees them
  on whichever thread runs it. On a worker thread that prints
  `NativeState is unsendable, but is being dropped on another thread`. It
  shows in `tests/test_icon_picker.py tests/test_editor_view.py` in up to 7
  of 10 runs depending on the collector's thresholds (`gc.set_threshold`).
- A popup's anchor that is not inside a text view takes the keyboard from
  that text view when it is pressed, as any press outside a listening view
  does; the popup then gives the keyboard back to nobody.
- Found on 2026-11-11 while testing nested Melty windows, present before and
  after the caller-following change: with an editor (`draw_editor`) in a Melty
  window and another in a Melty window declared inside it, the text of the
  editor in whichever of the two windows was not clicked last is drawn as
  garbled glyphs, and that window is dimmed. Input and values are right. Not
  investigated.
- Initial declarations and CPU state storage remain O(number of declared views).
  The benchmark mounted 10,000 rows in roughly 0.4 seconds. It is not a virtualized
  declaration/provider API.
- Nesting retains independent state and packets in one host ImGui context. Automatic
  selection avoids mandatory per-view textures, but Python capture/declaration
  and populated layout-transaction costs remain. Soft residency budgets cannot
  evict visible/focused content; the hard texture budget remains.
- Large individual raw draw lists are not automatically tiled. Put scrolling
  content in cached child views; viewport-body drawing is fixed chrome.
- The legacy constraint solver still snapshots the whole solver for body/layout
  transactions. Warm scrolling avoids body execution, but cold/layout-edit costs
  need further work.
- The all-visible full-redraw CPU overhead is about 6 us per view after the
  [redraw fast paths](GUI_REDRAW_FAST_PATHS.md), against 0.7 us raw. The rest
  is Python declaration/execution orchestration and ImGui capture.
- Calibration is opportunistic on existing presentations; it does not wake a
  fully sleeping app. Its baseline covers the current RGBA8 GL cache operations.
  External ray-marching/CUDA/body work is not automatically included in replay
  measurements. An already-retained external texture can benefit both paths.
- Automatic selection is a bounded local heuristic, with roots and portals
  remaining raster boundaries. It is not a globally optimal cache partition.
  Tested translucent representation changes can differ by two channel values.
- A view that is no longer declared keeps its state for the session, so that it has it when it is
  shown again. A long list that declares only the rows in view therefore grows by one state per row
  ever shown: `draw_collection` measured 1.5 to 3 KB a row (140 MB after 45,000 rows were scrolled
  past), and the collector's passes over them reached 43 ms once. The rows in view stay at about
  1 ms a frame.
- Anonymous identity is intentionally sensitive to conditional omissions and
  insertion/reordering of anonymous calls to the same renderer. Names change
  identity unless a key overrides them. Module/qualified-name changes need an
  explicit stable `state_id` when continuity matters.
- Arbitrary mutable input is not content-hashed or watched automatically. Use
  explicit invalidation for external in-place model changes. Strings longer than
  4,096 characters (`PATH_ALIAS` in `retained_gui_prototype.py`) alias by
  identity like any other object, so a whole file's text is never hashed or
  compared per declaration: pass a new string, or a model such as `Source`.
- The wake handle rouses GLFW and native Wayland. The iOS host has no entry
  point to request a frame without the interpreter, and this host draws with
  OpenGL only.
- Coordinate conversion does not span different native OS surfaces. Rotations,
  scaling and a general transform system are not implemented here.
- Python body/signature edits have focused state-preservation tests. One
  faulthandler-instrumented live-edit run crashed while core files were being
  changed. Clean launches passed; general live-edit/native-module reload
  robustness remains unverified. The crash was not established to be a geometry
  or scrolling failure.
- Six legacy render-function pickle tests pass in isolation. Collecting them
  with chrome tests exposes `_LazyRenderFunc._fn` recursion, reproduced on the
  untouched baseline. Keep that test-order issue separate from prototype results.
- Coverage here is headless CPU plus Linux Wayland/OpenGL. It does not establish
  CUDA, macOS, Windows, release-wheel or native-ABI hot-reload correctness.

## Code and test map

| Area | Entry points |
| --- | --- |
| Public decorators/signature compilation | [gui_prototype.py](../meltygui/core/rendering/gui_prototype.py) |
| Native state, argument resolution, serialization | [lib.rs](../native/gui_prototype/src/lib.rs) |
| Stable identity and save eligibility | [identity.rs](../native/gui_prototype/src/identity.rs) |
| Relative geometry and spatial indexes | [geometry.rs](../native/gui_prototype/src/geometry.rs) |
| Retained reconciliation and scheduling | [retained.rs](../native/gui_prototype/src/retained.rs) |
| Capture and composition | [texture.rs](../native/gui_prototype/src/texture.rs), [Python bridge](../meltygui/core/rendering/retained_gui_prototype.py) |
| Automatic raster policy and shared replay | [gui_cache_policy.py](../meltygui/core/rendering/gui_cache_policy.py), [shared_replay.rs](../native/gui_prototype/src/shared_replay.rs), [global toggle](../meltygui/core/runtime/toggles.py) |
| Runtime cost calibration | [gui_calibration.py](../meltygui/core/rendering/gui_calibration.py), [native probes](../native/gui_prototype/src/calibration.rs) |
| Window/input/layout integration | [gui_window_prototype.py](../meltygui/core/rendering/gui_window_prototype.py), [gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py), [gui_layout_prototype.py](../meltygui/core/rendering/gui_layout_prototype.py) |
| Keyboard focus and temporary popups | [focus.py](../meltygui/core/input/focus.py); the `popup` branch and `_fit` in [retained_gui_prototype.py](../meltygui/core/rendering/retained_gui_prototype.py); [example](../examples/gui_popups.py), [tests](../tests/test_gui_focus.py) |
| Wake from native threads | [wake.rs](../native/gui_prototype/src/wake.rs), `waker` / `drain_wakes` / `close_wakes` / `park` in [glfw_utils.py](../meltygui/core/windowing/glfw_utils.py), `poke` in [window_api.py](../meltygui/core/windowing/window_api.py) |
| Largest user so far | the [native editor](NATIVE_EDITOR.md): [editor_view.py](../meltygui/view/editor_view.py) |
| Default views | [gui_any.py](../meltygui/core/rendering/gui_any.py) (`draw_any`, `is_default_for`), [keyed.py](../meltygui/core/rendering/keyed.py) (`Keyed`, `keyed_as`), [value_view.py](../meltygui/view/value_view.py), [dict_view.py](../meltygui/view/dict_view.py); tests [routing and rows](../tests/test_gui_any.py), [views](../tests/test_gui_any_view.py) |
| Session persistence | [app_session.py](../meltygui/core/runtime/app_session.py), [load_save_v2.py](../meltygui/core/conversion/load_save_v2.py) |
| Contract tests | [a view follows its caller](../tests/test_gui_immediate.py), [identity](../tests/test_gui_identity.py), [persistence](../tests/test_gui_persistence.py), [positions](../tests/test_gui_position_api.py), [scroll/geometry](../tests/test_gui_scroll_geometry.py) |
| Interactive and measured checks | [scroll lab](../examples/gui_scroll_lab.py), [benchmark](../tests/benchmark_gui_scrolling.py) |

Run graphics tests and the lab on a reserved agent desktop with isolated session
paths, following [Development](DEVELOPMENT.md) and the machine's desktop rules.
