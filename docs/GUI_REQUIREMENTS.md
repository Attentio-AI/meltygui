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
| `input_value` is to have no special meaning to the framework: "Other parameter names are fully general, and you can almost replace input_value with a custom name now." Worked down case by case; one counter under both names, to see what still differs: [examples/gui_value_name.py](../examples/gui_value_name.py). | 11-16 |
| State injection is to work for a view's value too, to cut the boilerplate of prototyping a view: "start with injected state during the prototyping phase, and then add pass our own state later on." | 11-16 |
| A returned `(True, value)` stands in for a write to the first parameter's state: "the return value being a standin for draw_state.first_arg = new_val. That's conceptually clean in my head." Writing it by hand stays: "draw_state.input_value_one = 3 should still work and be an alternative to return value." | 11-16 |
| Positional arguments bind to the parameters in order: "draw_thing(input_value_one, input_value_two) just work with the return value being the only difference in behavior." | 11-16 |
| Invalidation is not part of that work: no change to it "unless it can be done without a performance impact." | 11-16 |
| A returned edit writes the state only when no value was passed: "Return writes only when no value passed." A passed value's edit is the caller's (the model would otherwise be held and pickled with the session). | 11-16 |
| Next, not part of this: values a caller passes are no longer saved with the session ("3 sounds like a good idea regardless, we can handle that next though, keep this update targeted"), and, as a step of its own run against the apps, a view that was passed nothing is no longer tracked under `None`. | 11-16 |
| `draw_state` holds every parameter, passed or not: "Values are always held on draw_state including when the caller passes those values." It "should cover all passed argument, make the first value as non special as possible. First value is special only in that the return can write to it." Raised by context-menu actions that read the view they are on (section 9). This takes back the holding half of the 11-16 row above ("the model would otherwise be held and pickled"); the saving half waits: "Final persistence can be put on hold as well, will work on that later." | 11-17 |
| The state must not keep a passed value alive: the reference holding is a "big issue for tensor type objects". "Make draw_state.input_value do a getattr and pull the value from the stack. That way the value is scope to the stack and has a natural deference every frame." For a cached view, which is not on the stack between its runs, the read goes to the cache's record of the view, which is pruned as a window is: "draw_view(melty_window=True) draws a window and that window lives longer than the single frame. Not calling draw_view prunes the window. This issue has a similar feel to it." Approved: "go ahead with the readthrough rework." | 11-17 |
| Built to the two rows above on 2026-11-17 (by Claude; **Lukas has not reviewed it**; a first build of the same day copied the passed value into the state's slot and is gone). `draw_state.<name>` reads every parameter; a passed value is read through and never copied. `NativeState.value` is its slot (told by `RetainedGraph.lend` when the node is made, and by every run) and `borrowed` (lib.rs) finds it on the runtime's stack while the view's body runs (`Frame.value`), else in the graph node's `input` (retained.rs, through a weak pointer): what the caller last passed, or the view's edit until the caller has answered it. The state adds no reference, nothing is released on retire, and what is saved is as it was. An immediate view's passed value reads while its body runs and raises `AttributeError` otherwise. Passed keyword arguments are still copied to their slots and saved, as before. Measured against the same tree without any of this (8 alternating runs each, headless, median and range): an immediate call passed an int 1031 (1008 to 1050) to 1032 (1017 to 1079) ns, passed an object 1022 to 1026 ns; a retained child that reruns 3660 (3587 to 3785) to 3739 (3656 to 3880) ns; declaring an unchanged child 1230 to 1221 ns. A body that reads its passed value: 1438 ns a call. (A first read-through build lent the value to the state around the body, two more state borrows a run, and cost an immediate call about 50 ns; the stack carries it now.) Which objects stay alive is the same as before the change in every case probed (a retired view, a value replaced, an immediate view no longer called). Open: a passed keyword argument is kept alive by the state of a view that is gone (as before; the persistence work); a view scrolled out of view keeps the value of its last run in the graph and hands it back to its caller (`d10832fa4c06`, found on the way, not caused by this). Reviewed by a second agent with no part in the work, as asked ("Have another fresh fable 5.2 subagnet review your work after completion"): no leak, crash or change to what is saved; four findings, all taken up. A view declared but not yet run (scrolled out of view) now reads its passed value; a source edit that moves the value to another parameter is followed (`reconfigure`); the error of an immediate view at rest names the attribute; the missing tests are written. Decided while building, his to overrule: a cached view that is gone reads its own value again (it raised in the first read-through build; before all of this it read its own), and a write while a value is passed is kept underneath and shows once the caller passes nothing, the read giving the caller's meanwhile. After those fixes, against a baseline that predates some of the other sizing edits to the same crate (so indicative only): immediate call 1029 to 1011 ns, retained rerun 3686 to 3708 ns, an unchanged child 1224 to 1223 ns, creating a child 14826 (13462 to 17130) to 14981 (13625 to 15724) ns, which now includes one call that ties the state to its node. Tests: [test_gui_value.py](../tests/test_gui_value.py). | 2026-11-17 |
| Built to the 11-16 rows on 2026-11-16 (by Claude; **Lukas has not reviewed it**). The first parameter is the value under any name; a value passed by name is the same value as one passed by position (tracked by the cache alike); a first parameter the framework fills (`FILLED`, gui_prototype.py) is not a value. Nothing passed is `ABSENT` (`inspect.Parameter.empty`), not None, and the value then resolves in the loop every parameter resolves in (`invoke`, lib.rs): state, decorator default, injected `DictConversion`, signature default, None. A view that owns its value has no held edit and nothing to settle, and runs once more after its edit to show it (`_propagate`). Decided while building, his to overrule: a value without a default is None when nothing is passed (it raised for any name but `input_value`); a view without a value parameter still drops a value passed by position, as before. Measured against the commit before (alternating runs, headless): declaring an unchanged child 1644 to 1276 ns (2000 children; the gain is a scan of the children that a parent's commit no longer makes when they are the same), immediate tree 627 to 609 us, retained full redraw 999 to 1003 us (341 views), an unchanged frame the same; a `str` value passed by name now costs what one passed by position does (1680 to 2045 ns), since both are tracked as paths. How to use it: [guide, sections 2 and 5](GUI_VIEW_GUIDE.md#2-the-contract-of-a-view); tests: [test_gui_value.py](../tests/test_gui_value.py). | 2026-11-16 |
| A value that is passed nothing and has no default is built by its type, not None: "The framework passed none for required val which is not ideal. I'd like to use the default constructor for the type instead." It is built once and then held: "the constructor only needs to run" once. This overrules "a value without a default is None when nothing is passed" in the row above. | 11-17 |
| That holds for every parameter, not only the value: "Lets do every parameter. That fits cleanly into the framework." A parameter without a type is None: "None for parameters without types sounds correct, do that." | 11-17 |
| "A constructor that needs arguments should call draw_function on the constructor", an `@gui` function: "draw_function exists as a render function already, we need an @gui version. Its a pretty simple idea, and should be really clean to implement with @gui. you call draw_any with a dict of the parameters of the function, then draw_button with a button that executes the function. I'd like draw_function to be a public reusable function at meltygui.rust.draw_function". | 11-17 |
| Built to the three rows above on 2026-11-17 (by Claude; **Lukas has not reviewed it**). A parameter without a default takes its annotation as the factory an injected `DictConversion` already used (`compile_plan`, gui_prototype.py), so it is built once per view and resolves where that did: after the state and the decorator default. `meltygui.rust.draw_function` ([function_view.py](../meltygui/view/function_view.py)) is `draw_any` of a dict of the function's parameters, which the view keeps as its state, and `draw_button`. A view with a parameter whose type needs arguments shows it in its own place until the button has built one, which is then the view's state (`asking`). Decided while building, his to overrule: `object`, `Any`, a union, an abstract class and a protocol build nothing and are None, like no type; a name the framework fills is not built; a held value that is no instance of the annotation any more (the annotation was edited, or the session saved an object it cannot pickle as None) is built again, so a plain class is built, or asked for, again at each launch; whether a type needs arguments is read from its signature (`np.ndarray` does, and is asked for), a C type that shows none is taken to need nothing, and a type that then raises when called with nothing fails the call with a `TypeError` naming the view and the parameter; the call of a view that is still asking returns `(False, None)`; `draw_function` calls in its own run (no thread), returns `(True, function)` in the run of a call that returned, keeps what it returned as its `result` state and shows an exception under the button; it is not registered with `draw_any` for functions. Measured against the tree before (8 alternating runs each, headless, median and range): an immediate call passed an int 1048 (1014 to 1122) to 1026 (1008 to 1087) ns, passed nothing 1073 to 1071 ns, with an injected `DictConversion` 1071 to 1069 ns, a typed parameter that is now built 1073 to 1071 ns; a retained frame of 2000 children, best frame per child, unchanged 2278 (2202 to 2821) to 2252 (2186 to 2478) ns, all rerunning 3806 to 3732 ns. Run on a reserved desktop (native Wayland, OpenGL): [examples/gui_function.py](../examples/gui_function.py) drew both forms, called the function, built the object, and after a relaunch had the counter's edit and asked for the object again. How to use it: [guide, sections 5 and 12](GUI_VIEW_GUIDE.md#5-state); tests: [test_gui_value.py](../tests/test_gui_value.py), [test_function_view.py](../tests/test_function_view.py). | 2026-11-17 |
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
| A hosted app's imports are managed for it: a package the host lacks is found in the `.venv` of the app's project (`gui_module.adopt`). "Should be able to auto manage imports." | 11-16 |
| `gui_module` must run a complex legacy app, the code editor, unmodified and with the bridge off: "That wrapper should be powerful enough to run the codeeditor unmodified. Code editor runs on its own, so it should be able to run inside the @gui wrapper. That's what I mean by a stress test of the module wrapper." `MELTY_RUST` stays off by default: defaulting it on was tried and taken back the same day, since under the bridge a right drag in the Rust tile view resizes the OS window instead of the tiles. ImGui state leaking between the hosted app and the host waits: "just want to get it rendering". | 11-16 |
| The Desktop's detected entry points are the wrapper's test set: "try embedding more entry point detected tasks into tiles like how we did for code editor ... Goal is to get them running well in the module wrapper with no changes to the apps themselves." | 11-16 |
| Each instance of a hosted module has a scope of its own for injected state: "I'd like the wrapper to create a local scope for each instance, mirroring how draw_state is unique for each instance. This should feel clean and natural within the framework." A legacy root's identity is under its hosting view's (`unique_name`), so its draw states and injected state objects are per instance. | 11-16 |
| A floating window is `melty_window=True`; a temporary one is `popup=` (section 9). | 11-11, 11-12 |
| A view given no width or height fills its parent: "Lets set the defaults to be None for width and height, default to filling the parent for views that don't specify." Superseded the same day in its spelling by the next row (the default is FILL, None is wrap). | 11-16 |
| Fill and wrap are spelled the same on both axes: nothing passed is fill, `None` is wrap ("I like passing none for its simplicity and compatibility, no imports needed"), a number is pixels. The fill override is "any negative number. Best practice for a fill override with be draw_view(width=-1, height=-1)", with public constants `meltygui.FILL = -1` and "for completeness" `meltygui.WRAP = None`: "the constants need not be used, but can be if so desired." Built by Claude, **Lukas has not reviewed it**: a wrapped width is measured like a wrapped height and is at most the room a fill would have; its body is told that room as `width`; a filling view inside a wrapping parent wraps; a window or popup that fills is 300 by 100 (`UNSIZED`). | 11-16 |
| The fill override is a very large number, not a negative one: a view whose computed size shrinks below zero must not jump to filling its parent ("That's not good"). "replace FILL=-1 with any large number. width=1e8 means fill the parent"; that a view which really is very large then fills instead is "a reasonable tradeoff for having an easy to type no import necessary interface." Built by Claude, **Lukas has not reviewed it**: `meltygui.FILL = 1e8`, any number from 1e6 up fills (`FILL_FROM`), a negative size is floored as before, and a width between 8192 and 1e6 still raises. | 11-17 |
| A body's `width` and `height` are always numbers, the space it has to draw in ("the view implementation not having to know or think about what the parent wants in terms of auto sizing. Just use width directly"), and a `canvas` parameter "holds the actually passed width/height parameter values". A height that is naturally unbounded is given as a large value: "it encourages the view to behave in a way that is natural for scrolling elements without making the view think about scrolling enabled or disabled state." Built by Claude, **Lukas has not reviewed it**: on a wrapping axis the number is the parent's room, 8192 (`UNBOUNDED`) where nothing bounds a height; `canvas` is read only; `draw_editor(height=None)` is as high as its text up to that room; content taller than 8192 is cut at `max_height` instead of raising. | 11-17 |
| A window's size follows what the call passes, "for width and height independently": nothing passed, "window is re sizable"; a number, "I would expect fixed height, variable width" (it used to snap back to the number while still letting a drag resize it); `None`, "I would expect resize not to work, instead window always fits its contents"; `1e8`, the window fills "the remainder of the display ... Bottom of window becomes fixed to the edge of the display", which "involves the collision system, I'd rather not handle it yet. Just keep in mind." He wants the same for OS windows ("os windows too actually"). Built by Claude for Melty windows, **Lukas has not reviewed it**: only a size on the call pins (a decorator size stays the starting size, as the existing windows and tests use it); a wrapping window is laid out in its parent's size. A fixed or wrapping axis takes part in collision like a span at its minimum: "the fixed heights and widths should not block the collision system from doing its thing. Min height and min width still allow the resize to push the edges of the window around ... This should work the exact same way" (a first version ignored the drag on such an axis; now it moves the opposite edge). **Not built**: the fill case, and OS windows, where `width=` / `height=` on `@os_window` are the starting size by the 11-13 row above ("static width and height is wrong"). | 11-17 |
| A size passed on the call wins over what the view's state holds: an existing window that was moved while its height was a number, then declared with `height=None`, did not grow past the old number until it was renamed ("this is a passed value not overriding the held state problem"). Built by Claude, **Lukas has not reviewed it**: on a wrapping axis nothing held pins the view, in its state or in the `width` / `height` its body is given (`RetainedGui.gui`, where `caller` is built). | 11-17 |
| A root window is given a smallest size (`min_width`, `min_height`), not a static one: "static width and height is wrong." | 11-13 |
| View sizing is "more robust and resistant to crashes": a width of zero or less, or a negative height, computed by a caller does not raise (task-runner's `status` crashed the app in a narrow window). Built by Claude, **Lukas has not reviewed how**: the view is drawn 1 px wide or with no height (`_fit_size`); non-finite sizes and widths over 8192 still raise. | 11-13 |
| A window has an absolute minimum size of 1 by 1 px, "so that this can't happen": `examples/gui_todo.py` raised `size must be finite and nonnegative` once the compositor made its window lower than the title bar (and again at every launch, from the saved size). The root's size is floored where it is handed out (`surface_frame.py` `Melty.root_fill`, `gui_window_prototype.py` `draw`, `gui_native_collision.py` `commit`), a Melty window's in its solver cell (`GuiGeometry.window`). A declared `min_width` / `min_height` above 1 still applies. | 11-15 |
| The title bar's height is never in app code. "I don't like sprinkling strip everywhere": a title bar wrapper "that leaves the inner view clean". The host puts the root's space under the bar (`title_bar` in `gui_window_prototype.py`): `top=0` is below it, `height` is what is left. | 11-13 |
| "I want all @gui functions to automatically be visible as part of the external interface", and "meltygui.view should be visible". A module-level `@gui` function named `draw_<thing>` is published as `meltygui.rust.draw_thing` with no hand-written row; `meltygui.view.<module>` loads on request. The IDE completes from generated stubs (`.pyi`): "it doesn't need to run all the time", one file at the repository root, `stubs.py`, regenerates them and the `VIEWS` table of `meltygui/rust.py`. | 11-16 |
| "I would like @gui functions to have better parameter hints in intellj and other IDEs (only testing intellj)", and "I'd like it to include the framework parameters as well". Built by Claude, **Lukas has not reviewed it, and it was not looked at in IntelliJ**: `stubs.py` writes each published view into `meltygui/rust.pyi` as a `def` with its own parameters and then the 20 names a call carries for the framework (`FRAMEWORK` in `stubs.py`: `FILLED` of gui_prototype.py without the 7 a body is injected with and the 2 a call rejects), with the view's docstring. Decided while building, his to overrule: a parameter after the view's first framework name is shown by name only; the return type is a plain `tuple` (`return_extras=True` returns three); Go to Declaration from `meltygui.rust.draw_x` now lands on the stub, not the view's source; a `@gui` function that is not published (an app's own, or a view imported from its module) has the hints it had before. | 11-18 |
| The parameter popup keeps the types: "You can put the types back, more info the better". Its one wrapped line "sucks", and IntelliJ has no setting for it, so the layout is a plugin's: "Lets make a intellj plugin instead. We have one in meltymeta, could add features to that." Built by Claude in `~/Desktop/melty-meta/intellij-plugin` (`hints/CallHints.java`, Melty File Tints 1.3.0; its README has the rules): a long Python signature is shown as the positional parameters in wrapped rows, then the by-name ones (for a view, `FRAMEWORK`) in four columns, types dimmed, the one being typed bold. It reads only the signature, so it knows nothing of meltygui. Seen in an isolated IntelliJ 2023.2 on a copy of `rust.pyi`; **Lukas has not reviewed it**. | 11-18 |

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
- **No text brightness clamp.** `@gui` officially supports very bright text:
  "No text clamp, new @gui to officially support very bright text." The
  `@render_func` renderer caps glyph pixels at `Toggles.HDR.text_max_stops`
  (`melty_clamp_text`); the Rust raster has no such cap and is not to get
  one. (2026-11-18)

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
  store. (11-06) For the app's own state he decided otherwise on 2026-11-14:
  a pickle per `@save` class, below.
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
- **An app's own state is a `@save` class**, the annotation of GUI Gym
  (`gui-gym/persistence.py`): it "feels cleaner than dictconversion. I'd like
  to adopt it for meltygui and make it the standard for app persistence."
  The class is the state and its attributes are the fields. The guide had
  `persisted(...)` and an injected `DictConversion` side by side, and an app
  used the first where the second would have done ("I see the gap now").
  (2026-11-14)
- **A pickle per `@save` class, beside the module that defines it.** "I want
  a pickle per store. That's better adherence to the locality rule."
  ([APPS.md](APPS.md#designing-a-feature): each thing lives next to what it
  belongs to.) This is the exception to "no second store" above: the views'
  state stays in the session, the app's own state is not in it. The first
  build of the same day put the class into the session; that is gone.
  (2026-11-14)
- **`@save(to_code=True)` writes the class back into its source through the
  Rust parser**: GUI Gym "has functionality to write back to a save file or
  write back to python source. This component should be modified to use the
  rust based syntax parsing instead of the python. We should already have
  rust parsing that we can reuse." (2026-11-14)
- Built to those three rows on 2026-11-14 (by Claude; **Lukas has not
  reviewed it**): `meltygui.save`
  ([app_save.py](../meltygui/core/runtime/app_save.py)).
  - The file is `<Class>.pkl` beside the module (`Tiles.pkl` next to
    `main.py`); `@save('name.pkl')` names it. Plain `pickle` of
    {field: value}, written whole or not at all at exit and at
    `meltygui.checkpoint()`, and only when its bytes changed.
  - It is read where the class is decorated, as in GUI Gym. Measured on a
    copy of tile-view (2026-11-14, 25 fresh processes): 0.66 ms in the
    decorator, of which 0.62 ms is the first `import pickle` of the process
    and 0.03 ms the read and unpickle of its 200 byte file; 0.02 ms when
    there is no file yet. To the first frame, 15 launches each in turn:
    272 ms with `@save`, 271 ms with a plain class, a difference inside the
    spread of either (257 to 294 ms).
  - To code, `Source.tree` finds the class and the assignment and an
    assignment to it splices the literal (`native/source/src/tree.rs`,
    nothing added to it); `compile` judges the result before the file is
    replaced.
  - Decided while building, his to overrule: a pickle that does not load is
    moved aside and the app starts from its defaults (the session's rule;
    GUI Gym raises); no `flush` / `flush_async` (nothing in meltygui saves in
    the middle of a run; `checkpoint()` saves everything now);
    `persisted(...)` and injected `DictConversion` view state keep working
    and are no longer taught for app state.
  - Open: whether an app's `*.pkl` belongs in its `.gitignore` (GUI Gym's
    does; tile-view's and the Hub templates' do not list it); where the
    pickle goes when the module's folder cannot be written (an iOS bundle).
- **Code written back is wrapped and indented**: of tile-view's tree saved
  with `to_code`, "The tree line gets long very quickly, I'd like the code
  to do a better job with indentation ... generally for code formatting not
  just this particular case." (2026-11-15) Built the same day: `wrap`
  (`model/tree_model.py`) lays out every list, tuple, dict and set that
  `Tree.put` or `@save(to_code=True)` writes. Inside `WIDTH` (120) it stays
  on its line; else its items go one `TAB` deeper, short ones sharing lines,
  one that holds lists or dicts of its own on lines of its own.
  - How to use it: [guide, section 5](GUI_VIEW_GUIDE.md#5-state).

## 7. Geometry and scrolling

- **Positions are relative; absolute ones are queries.** "The position
  attributes on draw_state should be relative not absolute. The absolute
  position of a view should be recomputable as needed without drawing the view
  graph... mid frame relative to different parents." (11-06)
- **`left=5` means 5 from the parent's content origin** on that axis. An axis
  that is not given follows the flow. (11-07)
- **`left=None` / `top=None` on the call follow the flow**, over a position
  the view holds. Lukas expected `left=0, top=0` to be the flow position and
  positions to be relative to it; shown what that would break he kept the
  coordinates: "Got it don't want that. Lets instead go with left=None,
  top=None to mean view positioned with the flow, keep the current view
  coordinate system relative to parent." (11-17, built by Claude, not reviewed
  by him: not passing an axis still leaves the held position standing; a view
  that stops being a window also loses its layout frame.)
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
- **Context menus are an interface point of their own**, not part of `@gui`:
  "Render func biggest problem was that we overloaded it with too much
  functionality." Adding one is to be natural, with no "performance impact or
  code complexity impact on views that don't have one". Proposed spelling,
  not settled: `@context_menu({"Label": callable})` above `@gui`. Nested
  views are covered, which `@render_func` lacked: a right click shows the
  inner view's options, a separator, then the outer view's. The design is
  being brainstormed; nothing is built. (11-17)
  - The direction "seems reasonable": the decorator only registers the items
    for the view function, a right click walks from the hit view up its
    parents, and the menu is an `@gui` popup view. (11-17)
  - The menu view, `draw_context_menu`, owns the injection of an action's
    arguments: "it would be reasonable for the draw_context_menu @gui
    function to own the argument injection. For menu lambdas with no
    arguments, just call them. Argument names are auto matched to the local
    scope in the view function." So `def delete_item(my_input, draw_state)`
    is given the clicked view's `my_input` and `draw_state`. (11-17)
  - An action reaching the view around it
    (`draw_state.parent.input_value.pop(my_input)`) needs two things. The
    state reading a value its caller passed: decided and built on 11-17
    (section 3). `draw_state.parent`, which `@gui` does not have (the parent
    is in the cache by node, `graph.parent`): "Lets do the parent addition on
    its own later." (11-17)
  - Built on his word "That sounds fine. Lets implement it", to the defaults
    put to him: the options are a dict; of a menu and a view that uses the
    right click itself the nearer takes it; the walk goes to the root; no
    Inspect row; after a pick the option's view and its parents run again; of
    immediate views only a window's root has a menu. And his one change: "find
    a way to attach the popup to the correct node in the graph." (11-17)
  - Built to those rows on 2026-11-17 (by Claude; **Lukas has not reviewed
    it**). `meltygui.context_menu` ([context_menu.py](../meltygui/core/input/context_menu.py));
    how to use it is section 7 of the guide, how it works
    [GUI_PROTOTYPE.md](GUI_PROTOTYPE.md#context-menus), the example
    [examples/gui_context_menu.py](../examples/gui_context_menu.py).
    - The popup is a child of the innermost view that has options: that view
      is run again, and a step after its body declares the menu with `popup=`.
      Only an immediate root's menu has no view; the surface declares it.
    - The one addition to the core is `AFTER` (gui_prototype.py): a step
      after a view's body, for the views that have a row. A view without a
      menu is bound and run as before; an app without one pays a truth test a
      frame. Measured for views without a menu (16 alternating headless runs,
      median): an immediate call 1011 to 1030 ns (ranges 993 to 1057 and 1001
      to 1097), a retained rerun 3828 to 3806 ns, an unchanged child 1255 to
      1257 ns, creating one 14668 to 14602 ns. The per-frame tests were not
      measured.
    - Decided while building, his to overrule: `MENUS` and `AFTER` are by
      view name, so a source edit's options are read by live views; the menu
      view is `meltygui.rust.draw_context_menu`, while `meltygui.draw_context_menu`
      stays the `@render_func` inspector; a right click elsewhere while a menu
      is open opens it there, and one inside the open menu does nothing; an
      option that raises is printed and the menu closes; the hovered row is
      drawn in the selection colour of `look` (its `on` did not show).
    - Seen on an agent desktop (native Wayland, OpenGL): the example's three
      sections with their lines at the pointer, the hovered row, a pick by
      pointer (`Upper case`, `Delete`) and by the arrows and Enter, Escape,
      the root's menu over empty space, a note in the scrolled list, and a
      menu at the bottom edge moved up to stay on the surface. A right drag
      and a press outside were checked by the tests only.
    - Open: an immediate view other than the root has no menu, and the
      root's options are given `cache` only; a decorator taken off by a
      source edit leaves its options until restart; closing a menu opened
      inside a popup that takes no keys closes that popup too, a defect of
      the popup rules found on the way (`52bf7426ee1f`).
- **Pointer shapes**: the text cursor stays through a selection drag; the
  editor's gutter shows the pointing hand. (11-11, 11-12)
- **Tiles are `meltygui.rust.draw_tiles`**, a reusable view that works like
  the `@render_func` tile manager, modeled on Blender's. A join can be turned
  round: once it is started, dragging back over the tile the drag began in
  joins in the opposite direction, "that's how it works in blender". Joining
  only a neighbouring tile of the same split, and a new tile being a copy,
  are Blender's behaviour too, not defects. (11-14)
- **The tiles need no cell around them.** Their caller places them like any
  view; an edge of theirs "within 100px of the window edge" collides with it
  automatically (`anchor` in gui_layout_prototype.py). (11-14)
- **A tile picks its view.** `draw_tiles` takes "a list of @gui functions, and
  then display[s] them as a dropdown selector in each tile. That tile will
  then invoke its selected view function." The list is `views=`, which
  replaces `tile=`; the pick is kept in the tile's dict as the function's
  name. (11-15)
- **No margin for pushing the window.** "If I remember correctly, we have a
  100 pixel margin for pushing on parent edges. Lets start by removing that."
  `anchor` takes no reach (this replaces "within 100px" above). (11-17)
- **A placed view is constrained to its parent, and the solve finds what has
  to move.** "I'd like the rules implemented as written. With a fixed sized
  view, growing the size of the inner tile should actually pull the left edge
  of the tile view, and that edge should pull the left os window edge ... The
  guiding principal of the collision system is that when the user request
  that something move, like the right edge of the tile window, we do what we
  can to make that happen without violating any of the constraints specified
  by the view layout. In this case, the size and position of the tile view is
  constrained relative to its parent. To move the right edge we must move the
  parents left edge. Moving the parents left edge should be automatically
  discovered by the collision solve." `anchor` gives the view a fixed gap to
  its window's near edge and a fixed span; a view that ends on the window's
  far edge is tied to that edge instead (it fills). The rule is in
  WINDOW_COLLISION_COLUMNS.md, "Views placed in a window". (11-17)
- **Wrapping is part of the collision solve.** "This case is instructive.
  draw_tiles(width=None, height=1e8). In this case height is the default fill
  behavior, width is wrap contents. Wrapping contents for a tile view means
  the minimum width the collision solving can find. I would like wrapping to
  fit elegantly into the collision tiling system." A wrapping axis of a view
  with rows or columns is solved to its cells' minimums as its layout closes
  (`GuiGeometry.wrap` / `shrink`), which is also the size the view is measured
  at; it then holds like a number. A tile can be split along it: the view
  grows by the new tile. Open: a wrapping tile view runs again on every frame
  its window's width changes, and the left edge pointer feedback (agent seat)
  was left alone as no obvious, clean fix was found. (11-17)
- **The solver does not author a placed view's rectangle.** Lukas saw the OS
  window fly off screen after changing `left` / `top` / `width` between runs
  ("usually going from left=100 and top=100, then removing the parameters ...
  The issues go away when I reset the draw_state"). Cause, from the session
  of a stuck app: the collision solver placed the tile view with
  `graph.allocate`, which authors `left`, `top`, `width` and `height`; the
  session saved them, and authored fields outrank the flow position and the
  fill size once the call no longer passes them (left 100 and width 500 held
  against a call that asked to fill). A view `anchor` constrains is now
  written without authoring (`GuiGeometry.put`), and an authored rectangle
  from an older session is dropped the first time the view is anchored
  (`disown`). A window keeps authoring: its geometry is the user's. (11-17)
- **`draw_tiles` takes any dict.** "Have the tiles add id as necessary and not
  force the user to know how they work. Make sure a blank dict works by
  default. Simple nested dict structures should work by default as well.
  Assume horizontal splits if none provided. You can update the dict but it
  should not be required to memorize a specific dict schema to call the
  function." `{}` is one tile and `{'cells': [{}, {'cells': [{}, {}]}]}` a
  tree; `mend` writes the ids, axes and a blank tile for an empty split into
  the dict. A split with no axis is 'x' (side by side) at the root and the
  other axis than its parent's below, or nesting would mean nothing. The ids
  stay in the dict: a tile's position changes with every split and join, so it
  cannot key the tile's view state. (11-17)
- **`apply` places only the cells that changed.** "Lets make apply only look
  at cells whoes edges moved to start": writing the solved rectangles to the
  views (`GuiGeometry.apply`) ran once per executed body over every cell of
  the window, 11.4 ms of a 20.3 ms frame with 51 tiles. It now visits a cell
  when one of its edges or its owner's origin moved, it was bound anew, or the
  committed view is it or its layout's owner. (11-15)
- **A divider drag does not run `draw_tiles`.** "Lets optimize draw_tiles
  next... with the view guide in mind": it ran on every frame of a drag
  because a layout nested in a cell made the outer one count as hand drawn.
  Nested layouts no longer do. An owner that keeps its cells' sizes asks
  `layout.sizes()` and runs once when the drag ends (`GuiGeometry.settle`);
  an owner that does not ask still never runs for a drag. (11-15)
- **A split or join drag runs the dragged grip, not every grip.** "Lets
  optimize split and join next": a grip took the shared hand as its value, so
  one grip's edit ran all of them, and `draw_tiles`, on every frame. The hand
  is a model with `Readers` now: the dragged grip moves the divider or aims
  the join itself and wakes `draw_tiles` only for what needs the tree. (11-15)
- **A view that runs twice in a frame is marked on screen.** "Let's make it
  loud. Let's draw red rects around views that double rendered so it's
  immediately visible in the screenshots", with "a little red tag in the top
  right corner that says double render in small text". The executor counts
  runs per frame (`Executor.end_frame`), `RetainedGui.present` draws the mark
  for `DOUBLE_HOLD` seconds (levers: `DOUBLE_*` in
  `retained_gui_prototype.py`). Always on; layout waves are not exempt. (11-15)
- **A resize drag runs a view once a frame**, the first two fixes the mark
  led to ("can you fix the module double render now"). `wake_next` is served
  after the host's tree has run, only for a view nothing else ran
  (`RetainedGui.frame`); and while the layout drags an edge or a window, the
  pointer's moves run no view (`ImGuiInput.process`): presses and releases
  still do. (11-15)
- **A window resize runs the root once**: "when the size of the root window
  changes it triggers a double render." `draw_tiles` wrote the rescaled split
  sizes and reported an edit, and a reported edit ran the caller once more in
  the same frame. Fixed by the next line; `draw_tiles` reports the sizes as
  before. (11-15)
- **A reported edit does not run the caller a second time, and the guide has
  no drawing order rule.** Of "input first, then children, then drawing":
  "That's a bad rule and I never requested it. It should not be in the guide
  and it certainly should not be enforced by the framework." The refresh of
  the caller after a child's edit ("One signalled refresh", in
  `retained_gui_prototype.py` and `declare.rs`) is removed, and rule 4 is out
  of `GUI_VIEW_GUIDE.md`. A caller runs once for an edit; what it drew from
  the value before the child's call shows the edit on its next run.
  `draw_collection` opens a pressed branch within the one run. (11-16)
- **A right drag that resizes the OS window runs the root once.** "We are
  still having a double render problem ... right click drag resize of the
  main.py in melty-search, just the template project." The pointer moves with
  the drag, so the input pass ran the root at the old size and the host's
  declaration ran it again at the new one; the earlier fix held the pointer's
  moves back only for the layout's own drags. A frame whose window size
  differs from the last (`_GuiWindow.draw`, `resizing`) now holds them back
  the same way (`ImGuiInput.process`). (11-16)
- **A layout opened in less room than it was saved in is a window resize.**
  "This should be treated as a window resize during runtime ... we just need
  a collision solve at launch to correct for the size change." `declare` no
  longer raises `unsatisfied layout span` for that: the short layout pushes
  its far edge, then its near one, against the cells around it, and what the
  window cannot give grows the owner's frame past its rectangle, as shrinking
  a window under its layout's minimum does. (11-15)
- **The worst case, many views changing width at once, is to be efficient in
  the framework itself.** "This is a stress test of the collision system
  itself, to get good performance here the code needs to be really
  efficient." Measured on 93 tiles, 49 resized a frame (199 bodies): 14.5 ms
  a frame before, 12.1 ms after these, in interleaved runs. `apply` ends at
  once when no edge moved and reuses a cell's rectangle; a flush tests each
  dirty view hidden in a scroll view on its own, where it walked every
  visible view of the window (also on idle frames); a declaration outside a
  layout cell finishes natively in an app that has cells; the title bar's
  backend check is kept for 2 s. Open: a declaration whose size alone changed
  still resolves in Python (about 3 ms of the frame). (11-15)
- **A list of parameters is exempt from a new declaration.** "Rather than hard
  coding this for left and top lets maintain a list of parameters exempt from
  re-declaration." `PLACED` in retained_gui_prototype.py (`left`, `top`,
  `width`, `height`, `scroll_offset`, `content_offset`): a call that differs
  from the remembered one only in their values is placed again by `_settle`,
  the one function that decides a view's rectangle, also for a first
  declaration. Nothing public changes. Measured: 276 of 283 declarations a
  frame take it in the 93 tile resize, for about 0.3 ms of a 12.5 ms frame;
  the estimate of 3 ms above was wrong, most of that is what follows the
  resolution and still runs. (11-15)

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
- **`Source` takes what the code belongs to, and does its own file io**
  (wanted, not built; design under review first): `Source(Toggles)`,
  `Source(sample_function)` and `Source(a_module)` beside `Source(text, path)`,
  and `draw_any(source)` draws it. "This is an expansion of scope for source,
  as the constructor will now have to delegate the file io required." It is
  the step toward the Melty code editor in `@gui`: several source trees,
  saving and loading, conflict resolution, git history. (2026-11-17)
- **That usage code is the bar for every code feature**: it "is very
  intuitive and easy for me to remember, the end goal of this design is to
  make interfacing with code feel like that for all features in the code
  editor." The design is being done again for code file io; nothing is
  settled yet. (2026-11-17)
- **Two buckets of requirements, kept apart**: those "driven by how I want
  the interface to work and the mental model encouraged by the framework", and
  those "driven by performance constraints". (2026-11-17)
- **Pending saves are a performance workaround, not wanted UX.** "The entire
  pendingsave feature exists entirely because file io is slow, not because I
  like the UX of working with pending saves. In a perfect world where flash
  storage is as fast as ram there is no pending save. We have one code string
  object and editing that edits the file on disk." (2026-11-17)
- **Virtual files are under consideration** (an idea, not decided): file io
  and when to save happen in a layer the framework is wrapped in, so "the
  framework can stay pristine". His condition: files are read on the render
  thread with "performance on par with a direct read from a python str".
  (2026-11-17)
- **Versions of code the interface must make natural to read** (his list,
  "non exhaustive"): the code on disk at a path; the code at a specific
  worktree, and there at a specified git commit; the code being executed
  (disk diverges from the running app through changes on disk or through
  hotswaps, and this version "stays in sync with the code post hotswap"); the
  initial code at launch time. (2026-11-17)
- **The model is one object that is the file on disk.** "From the model side,
  I want the simplicity of one object that is the file on disk. Set the value,
  and done." Waiting for typing to rest is the view's business: it "can live
  inside the @gui render function for the text". (2026-11-17)
- **The launch version is not made by reading every file at launch**:
  "Reading every file on launch is not what we want." His first idea, open to
  a better one: "record the git hash and the current diff at launch. That
  give you everything you need to retrieve the original launch state the code
  and is cheap in nearly all cases." (2026-11-17)
- **Launch capture as a git tree, and a prototype to be built.** To Claude's
  variant (the working state stored as a git tree with its objects outside the
  repo, read like a commit; a file whose ctime is older than the launch is read
  from disk; the shared in-RAM text is the view's working buffer and the file
  object is what it sets): "That seems reasonable. Let move forward with a
  prototype implementation." It goes on the main meltygui repo, "in a way where
  I can test it side by side with the existing source object". The end goal is
  the usage code with the tree drawn: `draw_any(toggles_source.tree)`,
  `draw_any(function_source.tree)`, `draw_any(module_source.tree)` (this
  replaces `draw_any(source)` in the row of the same day above).
  (2026-11-17)
- **Built to the 2026-11-17 rows above** (by Claude; **Lukas has not reviewed
  it**). `Source(thing)` beside `Source(text, path)`, which is unchanged: a first
  argument that is no str gives a `Scope` (model/source_scope_model.py), a
  handle on the one text of its file (`File`, model/source_file_model.py) and
  the key path of the class, function or module in its tree. One Rust thread
  without the interpreter (native/source/src/file.rs) loads the file, writes
  the whole text behind every edit (temp name, rename, no fsync) and takes the
  changes of other programs from inotify as one splice. No save, no dirty flag,
  no pending text, no timer. `examples/gui_source.py` is the usage code
  verbatim; `examples/gui_source_side_by_side.py` shows both forms on one
  scratch file. How it works and what is missing:
  [native editor](NATIVE_EDITOR.md#sourcething-the-code-is-the-file).
  Decided while building, his to overrule:
  - `Source(thing)` returns a `Scope`, which is no `Source`; `scope.file` is the
    whole file's `Source` for `draw_editor`, `scope.text` the definition's
    lines (settable).
  - The owner of path to text is a module table (`source_file_model.FILES`),
    not `Melty.files`: importing `core.melty` where `Source(Toggles)` stands
    costs 14 ms on the main thread before the first frame.
  - The constructor reads nothing. A view draws an empty tree until the io
    thread woke the frame loop with the text; code outside a frame that asks
    for `.tree` or `.text` waits for it.
  - The file wins: an edit made on a text the file has left behind is dropped
    when the file's change arrives, with no conflict shown.
  - A write to a version that is not the file is ignored; to a statement's key
    (`tree['Knobs'] = 1`) it raises `TypeError`.
  - Versions are spelled `at=`: `Source(Toggles, at='launch')`,
    `at='HEAD~1'`, `at='disk'` (the default). One word to rename, in
    `Scope.__init__`.
  - The launch state is captured when the first `Source(thing)` of a repository
    is made (at import for the usage code), not by the app's boot; files over
    1 MB that differ from the index are left out.
  - A decorated function is the function it wraps, a property its getter, a
    name defined twice its last definition; something without a Python file
    raises `TypeError`.
  - The compiled copies in `__pycache__` are removed after every write.
  - Every edit is one write of the whole file, as fast as the disk takes them:
    a drag in a file of 8.5 MB writes 1 GB a second. A pause between writes
    in the io layer would bound it; none is built.

  Measured (medians; details in the native editor document): reading a line 112
  to 124 ns against 57 to 64 ns for a `str` slice and 114 to 130 ns for a
  `Source`; a one character edit 6.3 to 6.4 us on the render thread at any
  size (`Source`: 5.6 to 5.7 us; a `str`: 3.2 us to 0.65 ms); the file written
  0.10, 0.24 and 3.0 ms later at 1,000, 10,000 and 100,000 lines. Frames while
  a value is dragged in `draw_any(source.tree)`, a real window on a reserved
  desktop, the render thread's time a frame: at 1,000 lines median 3.3 and 4.2
  ms, worst 7.2 and 6.3 ms, none of 702 over 8.33 ms (`Source(text)`: median
  3.4, one of 381 over, 9.4 ms); at 100,000 lines median 3.3 to 3.9 ms and 15
  of 1,665 frames over 8.33 ms in four runs, worst 10.7 ms (`Source(text)`: 2 of
  638 in two runs, worst 10.1 ms). Whether the writer causes the difference at
  100,000 lines is not attributed. Launch of the usage code, first frame
  presented, interleaved: 295 to 318 ms (median 304, six launches) against 273
  to 329 ms (median 303, seven) for the same file without the three sources;
  the three constructors take 1.1 to 1.4 ms of it on the main thread. The
  launch capture takes 116 to 119 ms on its thread in this repository and
  writes 440 KB to a temp folder.
  Found on the way: the verbatim usage code shows only its first tree, since a
  view without a size fills its parent (three plain dicts do the same); fixed
  in `Source`: a store panic, a reading thread that died on a long non-ASCII
  token, `Already borrowed`, an older reading replacing a newer one, and a scan
  of every key for one that is missing (3.4 ms to 0.8 us among 100,000 names).
  **Not built**: the code being executed (`at='running'` raises: nothing keeps
  the text a module was compiled from, and no hotswap runs in an `@os_window`
  app), a class's file in another worktree, a conflict view, closing a file.
  The legacy stack (`PendingSave`, the roster, `@save(to_code=True)`) does not
  know about it. Tests: [test_source_file.py](../tests/test_source_file.py),
  [test_source_scope.py](../tests/test_source_scope.py),
  [test_source_version.py](../tests/test_source_version.py),
  [test_source_file_view.py](../tests/test_source_file_view.py). (2026-11-17)
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
  colour picker to 2000 nits (`e151bec592c4`; built on 2026-11-18 to the
  wider request in section 17).

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
| First parameter `input_value`, typed. | The first parameter is the value under any name, typed and optional; `input_value` is the convention (section 3, 2026-11-16). |
| Return an accurate `(changed, value)`; mutate mutable input in place. | Unchanged; `None` means unchanged. |
| Inject events; do not poll ImGui input in a cached view. | The `event` parameter (section 3). ImGui widgets also work in cached views. |
| No ad hoc `DrawState` fields. | State is the declared parameters and injected `DictConversion` objects; `no_save=` for runtime only ones. The app's own state is a `@save` class (section 6, 2026-11-14). |
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

## 17. Colour and HDR

- **The main `@gui` colour picker** (2026-11-18): "we need the full p3 color
  range. I'd like brightness values up to 10k nits, which is an expansion
  relative to @render_func. Please keep the srgb square and show the extended
  range in the quadrants outside the srgb square." It is to be "an example
  widget implementation for future views", built by following the view guide
  closely.
- **No text brightness clamp** (2026-11-18; section 4).
- **The picker's field is to scale with `nits`** (2026-11-18): "I'd like the
  nits argument to scale reduce the height of the hdr quadrants, max height
  is 10,000 nits, so the colors are to scale. I should be able to go down to
  0 nits, and have the srgb section get shorter and shorter." Built the same
  day (`scale` in color_picker_view.py): a stop is the same pixels in every
  picker, the band is `BAND` high at 10,000 nits and gone at the desktop's
  white; under white the sRGB square is cut from above at the value whose
  brightest channel shows `nits`, down to no field at 0. `nits` over 10,000
  is 10,000. This replaces "the band's top is `nits=`, turned into stops"
  below: the band no longer keeps its height.
- **The hue bar shows the most vivid colour of each hue** (2026-11-18): "update
  the hue bar to use the most vivid hue available, p3 hue", the far corner of
  the P3 quadrant on the square's top row. Built the same day (`vivid` in
  color_picker_view.py; 24 segments, lever `SHADES`). The bar still selects
  the sRGB hue of the square.
- **Brightness, white and nits: how it fits together** (2026-11-18, from the
  review of `~/Desktop/hyprland-hdr`; the compositor facts are read from its
  patch, the Apple ones are not run on a device).
  - A Melty colour is a multiple of the desktop's SDR white. Nits exist only
    as `value x white nits`, and are what the picker's labels show.
  - On the Hyprland fork the brightness slider is `exposure`, one linear gain
    on every source before tone mapping: `emitted = content nits x
    brightness`. Ratios to white hold at every slider position, and
    10,000-nit content still reaches the panel's peak from `peak / 10,000`
    brightness up. Lukas keeps this model ("Everything scales with the
    brightness slider"); moving the slider to SDR white instead was
    considered and dropped, since PQ content that does not follow the
    reported white would not dim, or would be dimmed twice.
  - Melty reads white from the compositor's preferred image description
    (`wp_color_management_v1`, the `luminances` event:
    `wayland_color.query_preferred`). The fork reports `sdr_max_luminance`
    there, without the brightness factor, so Melty's nits are nits at 100%
    brightness. Other compositors may report something else; without colour
    management the fallback is `Toggles.HDR.pq_reference_nits`, a guess.
  - The fork's HDR gain (`hdrbrightness`) multiplies every PQ source, Melty's
    surface included, so any value but 1 puts Melty's white above the
    desktop's. Lukas set it to 1. Measured then with the fork's capture
    plugin on an agent desktop (SDR white 377 nits, brightness 100%): Melty
    white 379.4 nits, `white(4.0)` 1,497.5 (1,508 expected), grey 0.5 80.3
    (80.7 expected).
  - "I want 10k nits to be the max brightness value on the color picker,
    everywhere." Colours brighter than the display can show clip to a white
    blob in the band, whose edge is that display's maximum: he wants that
    kept, as it shows where the panel ends.
  - Apple (for when `@gui` has a Metal path; iOS host only today, no macOS
    extended range path): the layer is extended linear sRGB, 1.0 is SDR
    white, and the system clips above the current headroom while the layer
    carries no tone-mapping metadata, so the blob and its edge work and
    follow the brightness slider. Apple reports the headroom as a multiple
    of white (`UIScreen.currentEDRHeadroom` / `potentialEDRHeadroom`,
    `NSScreen.maximumExtendedDynamicRangeColorComponentValue`) and, as far
    as known, no nits of white. Plan: one "nits of white" function (the
    picker's `white()`): the compositor's value on Wayland; on Apple `panel
    peak / current headroom` from a per-model peak table while the panel is
    the limit, the brightness fraction times the model's SDR maximum when
    the headroom is at its cap, a default otherwise. Where white is an
    estimate the caption leads with the multiple and marks nits as
    approximate, and the picker runs again when the headroom changes. A
    wrong estimate moves only the labels and the band's height; stored
    colours and the blob's edge do not depend on it.
  - Open, his to decide then: on Apple the band stretches as brightness
    changes (10,000 nits is more stops above a dimmer white); the
    alternative is to fix white at the model's full-brightness value.
- **The picker's maximum is a multiple of white, not nits** (2026-11-18): "I
  think we move to relative nits for the max display. Change it to the
  multiple, that will be more universally compatible." This replaces
  `nits=` and "10k nits ... everywhere" above. Built the same day:
  `headroom=` on `draw_color_picker` and `draw_color`, the brightest colour
  shown as a multiple of white in linear light, 0.0 to `PEAK`; `scale` and
  `extent` take it and no longer read the desktop's white, so the field has
  one shape on every display. Decided while building, his to overrule: the
  name `headroom`; `PEAK = 32.0` (five stops), which is 10,000 nits at a
  white of 312 nits, about 12,000 at the 377 of his desktop that day and
  about 8,000 at 250; the caption reads `8.0x  3016 nits  P3`, the multiple
  first and the nits from `white()`, which is a reading on the fork and a
  guess elsewhere. For scale, estimated and not measured: 10,000 nits is
  about 10 times the white of a recent Pro iPhone at full brightness (about
  1,000 nits), about 5 times its outdoor boost (about 2,000), and Apple
  grants far less headroom than that at full brightness.
- **The three pickers of `@render_func` are a `selector=`, not tabs**
  (2026-11-18): `draw_color_picker(..., selector=meltygui.SRGB)`,
  `meltygui.SRGB_PLUS`, `meltygui.HDR`; "Make hdr the default". Built the same
  day: the constants are in hdr_color.py and public on `meltygui`; each
  selector is a row of six plain functions in `SELECTORS`
  (color_picker_view.py), and `field`, `bar`, `scale` and `extent` take the
  selector. `HDR` is the old Wide tab (one P3 square, P3 hue bar, the sRGB
  part outlined), `SRGB_PLUS` what the picker was until now, `SRGB` the
  classic square, which has no band whatever the headroom. Decided while
  building, his to overrule: every field is the same width, so `HDR` and
  `SRGB` stretch their square over the strip's room too; a colour outside
  what a selector lays out is shown at its edge and left as it is until the
  field is used; grey keeps the bar's hue under a saturation of 1e-4
  (`FAINT`), since the P3 conversion gives white a saturation of 1e-16 and a
  hue of its own. Seen on an agent desktop, the three side by side on one
  colour: a drag in one moves the markers of the others. Open: those others
  run twice a frame during the drag, as any view does that is passed the
  object another view returned (`64c314dbf1f5`, a framework gap, not worked
  around).
- **A view upgrades the default constructed value of its type**
  (2026-11-18): `draw_color_picker()` with nothing passed crashed on the `()`
  its `tuple` annotation built. "If you support a type the view should
  support upgrading the default constructed value of that type." In the
  guide, section 5. Built the same day: a colour with fewer than three
  channels is filled up from `START` (white) and returned as an edit, in the
  picker and in the chip.
- Built to the first row on 2026-11-18 (by Claude; **Lukas has not reviewed
  it**): `meltygui.rust.draw_color` (a chip that opens the picker as a
  `popup=`) and `meltygui.rust.draw_color_picker`
  ([color_picker_view.py](../meltygui/view/color_picker_view.py), example
  [gui_color.py](../examples/gui_color.py), tests
  [test_color_picker_view.py](../tests/test_color_picker_view.py)). One
  picking area and no tabs: the sRGB square bottom left, the P3 quadrant
  right of it, the HDR quadrants above both, one drag across all of it. The
  layout and its maths are the `@render_func` picker's sRGB+ tab
  (`hdr_color.srgb_plus_linear`, `srgb_extension_coords`), reused.
  - Decided while building, his to overrule: the band's top is `nits=` on
    the call (10,000), turned into stops above the desktop's white
    (`wayland_color`: 40 times white at 250 nits); the hue bar is sRGB hue;
    the channels are `draw_float` rows of extended sRGB, alpha held to 0..1;
    the text under them is a hex code inside sRGB and nits with the gamut
    outside; the legacy delete button (returning None to unset a tint) and
    the view offset rows are not carried over; `draw_color` is not
    registered with `draw_any` for tuples.
  - The field's picture is an fp16 texture its view owns (`Picture`, ended
    with `cache.on_retire`): 1.7 ms to bake per hue change, measured; a
    colour outside sRGB costs 2.4 to 2.8 ms to locate when it is typed or
    passed in, nothing while the field is dragged.
  - Seen on an agent desktop (native Wayland, OpenGL; screenshots are SDR,
    so the band was seen clipped and its brightness on an HDR output was
    not): the picker inline, in a floating window and as a popup; drags in
    the square, into the P3 quadrant and the band (4359 nits P3 read back),
    the hue bar (also under grey, which stays grey), a channel drag, alpha
    with the chip's checker; a relaunch kept the colours; a drag on the
    picker's empty space moved its window. With a print in each body:
    nothing runs at idle, a field drag runs the field, the picker and the
    chips of that colour, not the hue bar.
  - Open: a picker opened as a popup runs twice a frame while it is dragged,
    as the picker of `gui_popups.py` does (`7d0919d13f93`, a framework gap,
    not worked around); a chip's own colour goes through a vertex colour, so
    it clips at `Toggles.HDR.vertex_range` (64 times white) where the field
    does not; the band's range is read when the picker runs, so a change of
    the desktop's white shows at its next run; no Metal path.

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
