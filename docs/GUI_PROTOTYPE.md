# Rust `@gui` prototype: design, use and implementation notes

This records the prototype developed on 2026-11-06/07. It is an opt-in experiment
alongside `@render_func`, not a completed replacement. The priority is a small,
fast runtime with plain Python view functions and reusable state injection.

The current implementation has stable identity, native draw-state persistence,
relative geometry, cached nested scrolling, and `left`/`top` positioning.
[Geometry, scrolling and benchmark details](GUI_SCROLL_PROTOTYPE.md) supplement
this overview.

## Short how-to guide

Build using the checkout's interpreter, then run an example:

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
| Hide a view | Keep the same objects in the session | Retire captures/contexts |
| Evict an offscreen capture | Keep state and declarations | Release captures/contexts |
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
on demand; resident content replays. Nested viewport textures compose bottom-up,
so depth still costs GPU passes. Offscreen capture subtrees have a soft
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

The latest positioning milestone passed **273 focused tests**, including GPU
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

## Known issues, limits and next work

- Initial declarations and CPU state storage remain O(number of declared views).
  The benchmark mounted 10,000 rows in roughly 0.4 seconds. It is not a virtualized
  declaration/provider API.
- Nesting consumes viewport textures and composition passes. Soft residency
  budgets cannot evict visible/focused content; the hard texture budget remains.
- Large individual raw draw lists are not automatically tiled. Put scrolling
  content in cached child views; viewport-body drawing is fixed chrome.
- The legacy constraint solver still snapshots the whole solver for body/layout
  transactions. Warm scrolling avoids body execution, but cold/layout-edit costs
  need further work.
- Anonymous identity is intentionally sensitive to conditional omissions and
  insertion/reordering of anonymous calls to the same renderer. Names change
  identity unless a key overrides them. Module/qualified-name changes need an
  explicit stable `state_id` when continuity matters.
- Arbitrary mutable input is not content-hashed or watched automatically. Use
  explicit invalidation for external in-place model changes.
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
| Window/input/layout integration | [gui_window_prototype.py](../meltygui/core/rendering/gui_window_prototype.py), [gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py), [gui_layout_prototype.py](../meltygui/core/rendering/gui_layout_prototype.py) |
| Session persistence | [app_session.py](../meltygui/core/runtime/app_session.py), [load_save_v2.py](../meltygui/core/conversion/load_save_v2.py) |
| Contract tests | [identity](../tests/test_gui_identity.py), [persistence](../tests/test_gui_persistence.py), [positions](../tests/test_gui_position_api.py), [scroll/geometry](../tests/test_gui_scroll_geometry.py) |
| Interactive and measured checks | [scroll lab](../examples/gui_scroll_lab.py), [benchmark](../tests/benchmark_gui_scrolling.py) |

Run graphics tests and the lab on a reserved agent desktop with isolated session
paths, following [Development](DEVELOPMENT.md) and the machine's desktop rules.
