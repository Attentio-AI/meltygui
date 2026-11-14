# Rust `@gui`: requirements and design decisions

What Lukas asked for and decided for the Rust `@gui` runtime, and the Melty
principles restated for it. Compiled by Claude on 2026-11-13 from his Claude
Code and Codex chats (2026-10-09 to 2026-11-13), the bug tracker, and
[CONTRIBUTING.md](../CONTRIBUTING.md), [AGENTS.md](../AGENTS.md) and
[APPS.md](APPS.md). **Not yet reviewed by Lukas.**

How to read it:

- A date in brackets is the local day he said it. Text in quotes is his, with
  spelling corrected. A rule with no quote is my summary of an exchange.
- "Approved" means an assistant proposed it and he agreed.
- This file says what is wanted and why. How it is built, and what works today,
  is in [GUI_PROTOTYPE.md](GUI_PROTOTYPE.md), [NATIVE_EDITOR.md](NATIVE_EDITOR.md)
  and [FRAME_PACING.md](FRAME_PACING.md). Rules for writing a view are in
  [GUI_VIEW_GUIDE.md](GUI_VIEW_GUIDE.md).
- The conversations are listed at the end.
- **Keeping it current** (agreed 11-13): when Lukas decides something about
  `@gui` in chat, the agent in that chat adds a dated line here in the same
  turn.

## 1. What `@gui` is

- **A Rust implementation of `@render_func`, built beside it.** `@render_func`
  stays intact. `@gui` has its own native draw state, identity, size
  calculation and cache. The reason: "Core render is really slow, and there's
  no real opportunity to optimize it. It's death by a thousand little things."
  (10-09)
- **The idea of a view is unchanged**: "transform ordinary immediate mode draw
  instructions into a reusable view by wrapping them in a python function",
  with "a flexible forgiving interface externally to users of the view, and a
  simple local scope to the draw commands". (11-12)
- **The destination is a code editor that is all `@gui`.** Views are ported one
  at a time against a working editor. A native view inside the legacy tree and
  a legacy view called from a native body both exist for that; the goal is
  everything native. (11-10)
- **The interface need not match `@render_func`.** Where it differs the legacy
  view is rewritten; `@gui` does not grow compatibility for it: "I don't want
  to weigh @gui down with legacy bloat." `draw_text` was reimplemented
  (`draw_editor`) for that reason: "the relative coordinates of @gui are a big
  win, don't want to move backwards with abs_left and top." (11-10, 11-11)
- **New views are built beside the legacy ones**, which keep working. (11-11)
- **The interface is not final**: "The interface design for melty is still
  open." The worktree is path finding; no interface is committed. (11-10, 11-12)
- **Merged back on 11-13**: `rust-editor-pathfinding` into
  `melty-rust-prototype`, so `~/Desktop/meltygui` carries the runtime that
  new projects install.

## 2. Performance

| Requirement | Said |
| --- | --- |
| 120 fps, 8.33 ms, for every interaction. Nothing blocks the render thread or the launch. | standing rule |
| Worst case, every view on screen redrawing: 500 views at 120 fps. "Goal is to have freedom to use @gui even for small views." | 11-10 |
| Thousands of views should be usable, such as a text editor where each word is an `@gui`. "The more we can shave off the more use cases we unlock." | 11-10 |
| The measure is overhead per view against the same drawing as raw calls, with all views visible (no culling) as the worst case. | 11-07 |
| Scrolling deeply nested content is cheap: cost follows what is visible and what changed, not the size of the tree. | 11-06 |
| `draw_any` routes with near zero overhead and draws large structures at a high frame rate. | 11-07, 11-12 |
| The editor holds its frame time at 10,000 lines and beyond: "More text didn't meaningfully degrade render performance... that needs to be core to the design." | 11-11 |
| A module view (`gui_module`) costs nothing over an `@gui` function. | 11-12 |
| Frame times are even. A slow one-off frame costs more than its milliseconds (section 13). | 11-12 |
| Calibration and persistence never slow the launch; the state file stays small. | 11-06, 11-07 |
| A framework change comes with benchmarks before and after: "we need before and after benchmarks to ensure there are no performance regressions." | 11-12 |

On 11-10 he judged the per-view cost good enough to start porting ("Meltygui
rust is finally fast enough"). Current numbers:
[GUI_REDRAW_FAST_PATHS.md](GUI_REDRAW_FAST_PATHS.md).

## 3. The public interface

| Decision | Said |
| --- | --- |
| `@os_window` is `@gui` plus an OS window: one decorator to remember, not two. | 10-09 |
| `input_value` is optional in a view's signature; returning `None` means unchanged. "That's an old friction point with meltygui." | 10-09 |
| Callers and definitions can both make up arguments: "I really love having flexibility on both the function caller side and on the function def side to make up new arguments without breaking the interface points." No list of known framework fields; every field is equally fast. | 10-09 |
| `left` and `top` are the one way to place a view, relative to the parent's content. `window_pos` is gone. | 11-07 |
| `name=` tells repeated calls apart (section 5). | 11-06 |
| Whether a view is cached is not the caller's concern (section 8). | 11-07 |
| Short imports: `meltygui.gui`, `meltygui.os_window`, `meltygui.imgui`. "imports clunky, @ tags clunky." | 10-09, 11-05 |
| Every `@render_func` is exported on `meltygui` automatically; no list to maintain. | 11-05 |
| The new draw functions live in `meltygui.rust` (`draw_any`, `is_default_for`, the value views). | 11-07, 11-12 |
| `@gui` stays small. A feature that can be its own decorator is one: `@is_default_for` is not a `@gui` argument. "I want to avoid overloading @gui with functionality." | 11-07 |
| View functions are Python. Rust is the machinery under them. | 11-07 |
| "The vast majority of @gui views should be public": named `draw_<thing>`, published by a row in `EXPORTS` of `meltygui/rust.py`, used as `meltygui.rust.draw_thing`. An app keeps its root, its model and what only it needs. | 11-13 |
| Code is not terse: readable steps over clever one-liners (the gallery's `flipped ^= ...` input loop was "hideous"). Floats are written `0.0`, not `0.` or `.5`. No trailing commas. | 11-13 |
| The input event API will change: "flexible, intuitive and easy to remember", close to `event.is_hovered()`, `event.left_mouse_up()`. | 11-13 |
| Built to that row on 2026-11-13 (by Claude; **Lukas has not reviewed it**): a view takes one `event` parameter and asks it (`event.left_mouse_down()`, `event.left_mouse_clicked()`, `event.left_mouse_drag()`, `event.is_hovered()`, `event.keys()`, `event.text()`, `event.is_focused()`; the same for `right_` and `middle_`). Asking is subscribing: there is no list of events, a view runs for what its body has asked about, and asking about the left button is what takes the left drag from the window. `listen=` keeps one value, `()`, for a view that only paints. `view_events` as input and `cache.imgui_input.reach` are gone (`event.reach`). Vocabulary: [event.py](../meltygui/core/input/event.py); how to use it: [guide, section 6](GUI_VIEW_GUIDE.md#6-input). | 2026-11-13 |
| A view's file imports through the package, never a neighbour by its bare file name, so the view can be imported from anywhere. The first guide test produced views that could not: "import issue is concerning". | 11-13 |
| A floating window is `melty_window=True`; a temporary one is `popup=` (section 9). | 11-11, 11-12 |
| A root window is given a smallest size (`min_width`, `min_height`), not a static one: "static width and height is wrong." | 11-13 |
| View sizing is "more robust and resistant to crashes": a width of zero or less, or a negative height, computed by a caller does not raise (task-runner's `status` crashed the app in a narrow window). Built by Claude, **Lukas has not reviewed how**: the view is drawn 1 px wide or with no height (`_fit_size`); non-finite sizes and widths over 8192 still raise. | 11-13 |
| The title bar's height is never in app code. "I don't like sprinkling strip everywhere": a title bar wrapper "that leaves the inner view clean". The host puts the root's space under the bar (`title_bar` in `gui_window_prototype.py`): `top=0` is below it, `height` is what is left. | 11-13 |

## 4. How every view behaves

- **Like an immediate mode widget.** A view shows what its caller passes,
  whether it is cached, drawn directly or deferred, in an OS window or a Melty
  window. It may hold an edit for smooth rendering but returns to the caller's
  value "within a few frames". "It's a much clearer mental model when you can
  trust the view to behave like an immediate mode widget." (11-11)
- **Returned values keep Python call semantics.** `(changed, value)` reaches
  the caller at the call site; an accurate `changed` is what invalidates.
  (10-09, CONTRIBUTING)
- **A passed value wins.** A named value is state held by the view; when the
  caller passes it, the passed value overrides the state on every frame it is
  passed. (11-12)
- **One ImGui context.** A context per cached view was removed: "renderfunc
  never had that... it was added without my knowledge." (11-07)
- **ImGui widgets are supported, and the framework's own views do not use
  them.** "The guideline is to support imgui widgets, but not use them in the
  framework's @gui views." `meltygui.imgui` calls are allowed anywhere; for a
  pure `@gui` view he prefers the input events (`listen=` when he said it,
  the `event` parameter since 2026-11-13). (10-09, 11-12, 11-13)
- **A dropdown in a framework view is a `popup=` view**, not an ImGui combo:
  "Use the @gui popup system that's integrated." That a combo's list does not
  work in a cached view is a bug that will be fixed (`c3582148b768`); the
  wider failure of windows ImGui owns (child windows, popups, tooltips) is
  filed with low priority (`fb94b9e2eed2`). (11-13)
- **Sharp pixels.** Layout stays fractional and positions snap when drawn: no
  fuzzy text, no title that jiggles as a window moves. (10-09)
- **Sub pixel text hinting**, as the Python renderer has, for ImGui text and
  the editor. Asked for on 11-12; the assistant reported it done that day
  (`Toggles.Fonts.subpixel`).

## 5. Identity

- **Never from the input.** "It should not rely on any information about the
  rendered input_value": not its type, contents, address or hash. (11-06)
- **Parent, renderer, then a discriminator**: `key=`, else a nonempty `name=`,
  else the occurrence among unnamed calls to that renderer in the parent's run.
  Named and keyed calls do not consume occurrences. (11-06)
- **Why the name**: "It has a natural harmony with the way most views are laid
  out. If you have two draw_ints chances are you need to name them to tell them
  apart." Occurrence is the fallback only, because under a condition "I don't
  want failure to call a function to shift the IDs of later calls." (11-06)
- **Two calls to one view in one body must work.** (11-06)
- **128 bit ids, no collision detection.** (11-06)
- **Rejected**: finding the call site from the caller's frame or source line.
  Not fast enough. (11-06)

## 6. State and persistence

- **One state object per view, with a small fixed core.** The rest is the
  view's declared parameters and injected `DictConversion` objects. The old
  `DrawState` grew organically; this one does not. (11-06)
- **Persistence is the original `draw_state` design**: "take the object and
  persist it to disk, explicitly excluding parameters." No snapshots, no second
  store. (11-06)
- **Pruned at launch, never in a session.** What was not rendered when the app
  closed is not kept: "Anything not rendered is naturally pruned, keeping the
  app state file small and launch time fast." A view hidden and shown again in
  one session keeps its state: "Draw_states and state objects should only be
  pruned when the app is closed and then opened." (11-06) Confirmed on 11-13
  as working as designed, also where it keeps a state for every row a long
  list ever showed (`9bc55a3503c3`): not an issue "until it becomes a
  bottleneck in real world use".
- **Geometry is ordinary state.** "There should be no copying of data around,
  and there should be no functions like move window. Width, height and position
  should be no different than any other function parameter." (11-06)
- **Precedence**: caller argument, then authored or `initial` state, then the
  decorator default, then the signature default. (11-06, approved)
- **His test case**: a Melty window's size and position survive hide and show,
  and an app restart. (11-06)

## 7. Geometry and scrolling

- **Positions are relative; absolute ones are queries.** "The position
  attributes on draw_state should be relative not absolute. The absolute
  position of a view should be recomputable as needed without drawing the view
  graph... mid frame relative to different parents." (11-06)
- **`left=5` means 5 from the parent's content origin** on that axis. An axis
  that is not given follows the flow. (11-07)
- **A move does not run a body**: the cached output is placed again. (11-07,
  approved)
- **Scroll containers use the cache.** Scrolling was "one of the slowest parts"
  of `@render_func`; here it changes composition and visibility only. (11-06)
- Relative coordinates and container scrolling were prototyped first as "the
  biggest risks for the project". (11-06)

## 8. Caching and invalidation

- **The user never chooses.** "My only requirement on the cache vs not cache
  is that the user doesn't have to think about it." No API for it. (11-07)
- **Chosen from cost.** Compare what a view costs to draw with what it costs
  to cache and take the lower. A cheap view never pays for a texture; a view
  whose single shader is heavy (ray marching) ends up cached. (11-07)
- **Calibrated per machine at runtime**, since RAM and GPUs differ: now and
  then, in the background, never at launch. Kept apart from the rest of the
  code: "build it in a way where it just works, can use the computed constants
  in the framework without having to think about all the environmental details
  and state." (11-07)
- **One switch**: `Toggles.Rust.auto_cache`. (11-07)
- **No hashing.** A view is dirty because it returned `changed=True` or because
  `cache.invalidate(thing)` named an object, path, draw state or id tied to it.
  "As a rule melty does not hash values to invalidate." (10-09)
- **Any view redraws without its parents.** In the old cache "you can't draw
  the parent without also drawing the grandparent"; here an invalidated view
  runs alone. (10-09)
- **Windows and the cache are one system**, by draw list replay. A window
  declared under a condition goes away because its declaring parent runs
  again, not because the window's own call is replayed (the old leak). (10-09)
- **A cached view runs again by itself while its widgets need it.**
  ([guide, rule 2](GUI_VIEW_GUIDE.md))

## 9. Windows, gestures, focus and popups

- **The collision and column system works in full under `@gui`**, "mainly
  for my sanity, not having it breaks muscle memory." It "has
  historically been one of the most brittle parts of melty", so it was built
  early. Collisions are solved once: the root window was a defect while both
  Python and Rust adjusted it. Rules:
  [WINDOW_COLLISION_COLUMNS.md](WINDOW_COLLISION_COLUMNS.md). (10-09)
- **Default gestures on every Melty and OS window**: left drag moves, right
  drag resizes. (11-12)
- **A view overrides the left drag only for a gesture of its own, over that
  part only.** Text selection replaces moving the window over the text; the
  empty space below the last line still moves it. A container does not take
  the pointer over its whole box to catch presses on a few rows: this was "the
  biggest miss" in the first `draw_any`. (11-12)
- **Nothing overrides the right drag.** "Right click drag should be robust and
  always work to resize windows/columns/etc." (11-11)
- **A right click acts on release**, because a right press may be the start of
  a resize. (11-13)
- **Window movement stays locked to the pointer**, without a frame of lag.
  (10-09, 11-12)
- **Temporary popups are first class.** "A way to do temporary
  melty_window=True melty windows in a clean way without lots of boilerplate
  state management", with a focus system "that interacts with the text focus
  naturally and feels like a first class component of melty". The result is
  `popup=<open event>` and `cache.focus`; no view keeps an open flag.
  (11-12, approved)
- **Pointer shapes**: the text cursor stays through a selection drag; the
  editor's gutter shows the pointing hand. (11-11, 11-12)

## 10. `draw_any` and `draw_collection`

- **Two pieces for the user**: `@is_default_for(...)`, which takes types, names
  and shapes, and `draw_any`, which is called like any view and routes by type,
  recursively. (11-12)
- **Its own decorator**, so it is "less entangled with the core system". (11-12)
- **Rust routes, Python draws.** The value views (`draw_int`, `draw_float`,
  `draw_bool`, `draw_str`) are small Python `@gui` functions. (11-07)
- **`draw_collection` draws anything keyed.** It began as dicts only, then:
  "Anything that looks like a dict should be renderable with draw_collection."
  (11-12)
- **Code is such a value.** `draw_editor(source)` and `draw_any(source.tree)`
  show the same source, and an edit in either appears in the other. (11-12)
- **Its views follow the gesture rule** of section 9. (11-12)

## 11. The editor and `Source`

- **Feature parity with `draw_text`**, built as a prototype to iterate on.
  Cosmetics such as glow and shadows wait; styling that carries meaning, such
  as symbol tints, does not. (11-11)
- **Required**: inline widgets, inline tints for objects, scopes, debugger
  support, error messages, live values, scope expand and collapse, code
  suggestions, import handling. (11-11)
- **Order**: architecture first, then features by how siloed they are, small
  refinements last. "It's useful to keep the code as small as possible for as
  long as possible." (11-11)
- **Inline widgets should be cheap to add**: they "use more code than they
  should, could be annotated functions like @gui mapped to types." (11-11)
- **One undo stack, the framework's.** "I really don't want two undo
  systems." (11-11)
- **Input**: new event types are fine "as long as they feel natural to the
  framework. Don't invent a new input handler." (11-11)
- **`Source` is Rust**: the text and what is derived from it. "Analyzing in
  python is slow and runs into GIL issues. Parsing usage and errors has
  historically been the slowest thing in melty." Rust is an accepted
  dependency; wheels for iOS, macOS and Windows are to be checked. (11-11)
- **Our own parser stays possible.** Where strict correctness is not needed,
  "home built parsers can be dramatically faster." (11-11)
- **`Source` is the standard value for a code file**; `str` is for small
  fields. (11-11)
- **Nothing is keyed by line number**: breakpoints and errors hold a statement
  key and survive inserted lines. (11-11, bug `1e417233ac0a`)
- **Several editors on one source.** "I have lots of multi editor workflows I
  need to support." (11-11)
- **Cross file symbols**: one roster per project, owned by Melty's shared
  runtime and injected. Python says which files belong and pushes current
  text; a Rust thread reads and indexes. (11-11, approved)
- **A tint comes from the nearest decorator** when decorators are stacked.
  (11-11, approved)
- **No visible stutter.** "Want to stay on top of the stutters": a
  performance regression is looked into when it appears. (11-11)
- **Not perfect yet**: "prioritize fixes with clean solutions." (11-11)
- **Descoped**: usage heat. (11-11)
- **Filed against the editor** (tracker, `meltygui`): errors shown as a gutter
  icon with a small window above the line, not inline while typing; teal spell
  underlines suited to code; red argument names; symbol colours and parameter
  hints in suggestions; inline bools flip on a double click; a clean `@gui`
  colour picker to 2000 nits.

## 12. Module views (`gui_module`)

- **Why**: a fresh small project is easier to write and read, and "the python
  function is a mediocre language to describe a little app." The minimal shape
  of an app is "probably just a python module". (11-12)
- **Outside it is a function, inside it is a module.** Module globals are the
  local scope. The wrapper splits the module into load, draw and terminate
  code and removes its threads and windowing. (11-12)
- **A passed value overrides the module global of that name**, on every frame
  it is passed. Any global can be passed; none is special. (11-12)
- **Load runs off the render thread**, "probably on a background thread
  triggered on the first frame". (11-12)
- **The wrapped app is not edited to fit.** Its windowing calls (poll, new
  frame, render, swap) become stubs; its ImGui calls run in the host's
  context. (11-12, 11-13)
- **No overhead over an `@gui` function**, and a fast first frame. (11-12)
- **No runs without cause**: a hosted app that is idle does not invalidate.
  (11-13)

## 13. Frame pacing

- **Draw as late as possible**, so input is read close to the flip. Target:
  about 6 ms from input to screen at 120 Hz, without jitter. (11-12)
- **The guard adapts both ways.** "Growing only is not ideal. If the
  environment improves it would be nice if we eased back in to a low latency
  mode." After a slow frame it recovers in about 500 ms. (11-12)
- **Any refresh rate**, not 120 Hz hard coded. (11-12)
- **Latency does not climb over a session.** "Slow frames permanently damage
  the performance" was the defect. (11-12)
- Not confirmed fixed in chat: launches that latch at 16 ms until the window
  closes. The last step (11-12) was a compositor change and a restart.

## 14. The Melty rules, restated for `@gui`

From [CONTRIBUTING.md](../CONTRIBUTING.md) and [AGENTS.md](../AGENTS.md), which
still describe `@render_func`.

| Rule | Under `@gui` |
| --- | --- |
| Models look like values; views are plain local functions; no view classes, no global host or focus lookups. | Unchanged. |
| First parameter `input_value`, typed. | Optional. When present it is named `input_value`. |
| Return an accurate `(changed, value)`; mutate mutable input in place. | Unchanged; `None` means unchanged. |
| Inject events; do not poll ImGui input in a cached view. | The `event` parameter (section 3). ImGui widgets also work in cached views. |
| No ad hoc `DrawState` fields. | State is the declared parameters and injected `DictConversion` objects; `no_save=` for runtime only ones. |
| `initial={"width":, "height":, "window_pos":}` for a window's start. | `initial={'left':, 'top':, 'width':, 'height':}`. A passed `left` / `top` / `width` / `height` still applies on every frame it is passed. |
| `@render_func(is_default_for=...)`. | `@is_default_for(...)` above `@gui`, from `meltygui.rust`. |
| Use Melty windows and controls. | `melty_window=True`, `popup=`; no ImGui combos. |
| Automatic invalidation; never repaint every frame to hide a stale dependency. | Unchanged: `changed`, `cache.invalidate`, `cache.wake` from threads. `live=True` or `use_cache=False` is not a fix. |
| Detect staleness by mtime, generation or identity, never by hashing. | Unchanged, and identity never reads the input. |
| Consume background results before drawing; no long work in a body. | Unchanged. Whole text work belongs in Rust; a Python worker gives way between small pieces. |
| Locality: a feature starts as one file and grows when a second user appears. | Unchanged. A module view takes it further: one module is one app. |
| Report and fix framework gaps; do not hide them. | Unchanged. Gaps go to the tracker with steps and code. |
| Both backends behave alike. | Unchanged; say which backend a check ran on. |
| Hotswap every definition in place. | Wanted. A body or signature edit is picked up; robustness is unverified, and a module view does not reload. |

Code style, from his global rules: beautiful, concise, fast, self explanatory.
Names are one clean term. Comments say how to change the code and where the
levers are, with the constants at the top. Immediate mode patterns wherever
possible. No U+00B7 in UI text or comments. A demo is the smallest tidy
working example.

## 15. How the work is done

- **Prototype first, and the riskiest part first.** Throwaway code is fine
  until it is measured: "treat it as a prototype, minimal implementation.
  Above all it needs to be small and fast." (10-09, 11-06)
- **Sketch a design before building it**; he reviews the plan. (11-06, 11-07)
- **Optimise from intent.** "Think about the intent of the code rather than
  the precise behaviors": a behaviour may change if that makes the fast design
  possible and the intent holds. (11-07)
- **Say when you depart from `@render_func`.** An architectural addition he
  did not ask for is a defect. (11-07)
- **Explain a surprising number** by removing one piece at a time. (11-07)
- **Hard to follow machinery is kept apart**, with clear rules. (11-07)
- **Sub agents only when he asks**, with a fresh context and a full brief.
  (11-11)
- **Bugs go to the tracker** with steps to reproduce and the code. (11-12)
- **Write it down after each milestone**: decisions, pain points, known
  issues, a short how to. (11-07, 11-11)
- **GUI Gym** exists "to characterize the differences between renderfunc and
  rust gui". It may not change the views it shows. (10-26, 11-04)
- **Where the pointer to the view guide lives**: in the `AGENTS.md` /
  `CLAUDE.md` of Hub's project templates (`meltygui-rust` in
  `thermostat/project_templates`) and of the meltygui repository. A project's
  README is not the place. (11-13)

## 16. Open

- **Design features.** `@gui` "in its current form is more suited for
  engineering prototypes than polished design": margin, padding, font weights
  and the rest of what a designer's mock needs. A list of about 30 requests
  was drawn up on 11-12 and exists only in that chat (`1ac5f70a`).
- **Enhancements from the SVG editor sketch** (11-12, chat `e8edf48d`): listed
  in chat, not filed.
- **Host gaps filed by agents** (tracker, `meltygui-rust-editor`, 11 open),
  among them: ImGui popups placed at the view's local position, no transform
  for zoom, wheel events without modifiers, a `scroll_offset` write that does
  not run a `scroll` listener, the 8192 px view limit. (State kept for every
  row ever shown is by design for now, section 6.)
- **A guide for building `@gui` views** (asked 11-12 as a skill, 11-13 as a
  guide): [GUI_VIEW_GUIDE.md](GUI_VIEW_GUIDE.md), with
  [examples/gui_todo.py](../examples/gui_todo.py) as its worked example. Next:
  Lukas reviews it, then tests it by having sub agents build views from it.
  First test, 11-13: an agent given only a task in an app project found the
  guide on its fifth tool call and followed it; its six views are in
  `~/Desktop/task-runner/gallery`. They import each other by bare file name
  and are not published, which led to the two rows on public views in
  section 3.
- **Found while writing the guide** (filed 11-13, project `meltygui`):
  - The view that opened a popup is not run again with the value it returned
    (`9ed33b19b8e3`). When it is fixed, the workaround comes out of the guide
    and its example (Lukas, 11-13; the bug's note lists the places).
  - A left drag on an OS window's empty space moves it only when the root holds
    a `columns` / `rows` layout (`78d4ccd04ec6`). Lukas expected this to work
    already.
  - An absolute `imgui.set_cursor_pos` in an immediate root, and `left` / `top`
    on a `use_cache=False` view, are window coordinates, not the root's space
    under the title bar. Not filed.
- **iOS**: the retained host has no Metal path yet. (11-11)
- **`Source` bugs** documented in `native/source/README.md`, to be worked
  through later. (11-11)

## Sources

Conversations read, by local date. Claude ids are the first eight characters
of the session; Codex ones are named by their Hub title.

| Date | Where | Subject |
| --- | --- | --- |
| 10-09 | Codex, "Assess Rust Core Render Refactor" | Motivation, flexible arguments, cache and window unification, `@os_window`, ImGui widgets, collisions, pixel snapping |
| 10-26 to 11-07 | Codex, gui-gym | Comparing `@render_func` with `@gui` |
| 11-05 | Codex, cleanslate | `meltygui.imgui`, public `@gui`, automatic exports |
| 11-06 | Codex, "Review Rust draw_state Persistence" and its two forks | Identity, state, persistence, geometry, scrolling, `left` / `top` |
| 11-07 | Codex, same forks; "Review Rust GUI Render Optimizations"; "Latest Work on Rust GUI Implementation" | Overhead, automatic cache, calibration, one ImGui context, first `draw_any` plan |
| 11-10 | Claude `1b4b088a`, `6d2679b0` | Performance targets, feature gaps, the path finding branch, porting order |
| 11-11 | Claude `7498e7b8` | Rewrite `draw_text` instead of adding compatibility |
| 11-11 | Claude `cd4cfcc9` | Editor priorities, undo, `Source` in Rust, roster, multi editor |
| 11-11 to 11-12 | Claude `80620191` | Immediate mode rule, window lag, frame pacing |
| 11-12 | Claude `4f7784ed` | Tracker bugs, gestures, cursors, error popovers |
| 11-12 | Claude `e8edf48d` | Ideal usage code (SVG editor), host bugs, new `draw_any` |
| 11-12 | Claude `1ac5f70a` | Focus and popups, design feature list |
| 11-12 | Claude `f50bc53a`, `36ba0094` | Keyed collections, `Source.tree` |
| 11-12 | Claude `c2ecfc08` | The 16 ms latch |
| 11-12 to 11-13 | Claude `ac03d3d4`, `692228ef` | Module views |
| 11-13 | Claude `b3affd01`, `7b6ecd9e` | Minimum window size, ImGui widgets and combos, pruning confirmed, the merge back, the view guide, this file |

What was read: about 500 of his messages from chats in `meltygui` and its
worktrees, cleanslate, gui-gym, codeeditor-melty, melty-editor, task-runner
and entry-points, and three Rust sessions that ran under thermostat; the
tracker's list of the 23 bugs filed for `meltygui` and `meltygui-rust-editor`
(their excerpts, not each in full).

Not read: the assistants' replies, apart from the end of each and the few that
settle what a short answer of his approved; long pastes (tracebacks, session
summaries) in full; pictures attached to messages; chats about Hub, the iOS
runtime and other projects.
