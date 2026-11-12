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
keyboard with its editor. A view that lists nothing (`listen=()`) costs one
dictionary truth test a frame. `cursor=shape` (a `core/input/mouse_cursor.py`
shape) declares the pointer's shape over the view without running a body: the
innermost declaring view around the hovered one wins, and the shape a press
began with is kept until every button is up. The contract is the module text of
[gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py).

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

## Known issues, limits and next work

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
| Wake from native threads | [wake.rs](../native/gui_prototype/src/wake.rs), `waker` / `drain_wakes` / `close_wakes` / `park` in [glfw_utils.py](../meltygui/core/windowing/glfw_utils.py), `poke` in [window_api.py](../meltygui/core/windowing/window_api.py) |
| Largest user so far | the [native editor](NATIVE_EDITOR.md): [editor_view.py](../meltygui/view/editor_view.py) |
| Session persistence | [app_session.py](../meltygui/core/runtime/app_session.py), [load_save_v2.py](../meltygui/core/conversion/load_save_v2.py) |
| Contract tests | [a view follows its caller](../tests/test_gui_immediate.py), [identity](../tests/test_gui_identity.py), [persistence](../tests/test_gui_persistence.py), [positions](../tests/test_gui_position_api.py), [scroll/geometry](../tests/test_gui_scroll_geometry.py) |
| Interactive and measured checks | [scroll lab](../examples/gui_scroll_lab.py), [benchmark](../tests/benchmark_gui_scrolling.py) |

Run graphics tests and the lab on a reserved agent desktop with isolated session
paths, following [Development](DEVELOPMENT.md) and the machine's desktop rules.
