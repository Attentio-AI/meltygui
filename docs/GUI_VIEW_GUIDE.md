# Building a view with Melty (`@gui`)

How to write a view, and an app made of views, on the Rust `@gui` runtime.
Read this before writing one. Every rule here was either run on 2026-11-13
(native Wayland, OpenGL) or is taken from the doc it links; "not run here"
marks the second kind.

- The worked example is [examples/gui_todo.py](../examples/gui_todo.py): 167
  lines, every pattern below in one file. Read it next to this guide.
- Why the runtime behaves as it does: [GUI_REQUIREMENTS.md](GUI_REQUIREMENTS.md).
  Reference for each feature: [GUI_PROTOTYPE.md](GUI_PROTOTYPE.md).
- A rule you cannot follow because the framework is missing something is a
  framework gap: file it (section 13), do not work around it silently.

**Running.** The runtime is in `~/Desktop/meltygui` (merged there on
2026-11-13) and in the worktree it was built in, `~/Desktop/meltygui-rust-editor`.
A project that installs `~/Desktop/meltygui` editable has it. Python 3.12:

```sh
~/Desktop/meltygui/.venv/bin/python examples/gui_todo.py        # from ~/Desktop/meltygui
```

Launch on a reserved agent desktop, never on desk-1 (`/home/lukas/AGENTS.md`).
Keep your run away from the saved state of the real app, which lives in two
places:

- **The session** (the views' state) is under the home: give the run a home
  of its own (`HOME=/tmp/<you>/home XDG_STATE_HOME=/tmp/<you>/home/state ...`).
- **The app's state** (`@save`, section 5) is a pickle beside the module that
  defines the class, and no environment variable moves it: run a copy of the
  app from a temp folder (`cp -r ~/Desktop/<app>/*.py /tmp/<you>/<app>/`,
  then `~/Desktop/meltygui/.venv/bin/python /tmp/<you>/<app>/main.py`). The
  same for an example: copy `gui_todo.py` out of `examples/` first.

## 1. The shape of an app

An app is three kinds of thing:

- **A model that is a value**: a dict, a list, a number, or a small class that
  behaves like one. It holds the data and nothing about drawing. What the app
  keeps between runs is a `@save` class (section 5).
- **Views**: plain functions under `@gui`. A view draws the value it is given
  and returns it, edited or not.
- **A root** under `@os_window` that only composes: it passes parts of the
  model to views and writes back what they return.

The root of the example, whole:

```python
@os_window(name='To do', app_id='meltygui-todo', width=520, height=420, min_width=320, min_height=200, use_cache=False)
def app(width=None, height=None, cache=None):
    top = PAD
    added, _ = button('Add', name='add', left=PAD, top=top, width=60.0)
    if added:
        Todo.items[str(Todo.next)] = {'title': f'Item {Todo.next}', 'done': False}
        Todo.next += 1
        cache.invalidate(Todo.items)        # changed in place, outside the views that show it
    picked, Todo.filter = dropdown(Todo.filter, name='filter', choices=FILTERS, left=PAD + 68.0, top=top, width=110.0)
    uptime(clock, name='uptime', left=width - 80.0 - PAD, top=top, width=80.0)
    top += TOOLS + PAD
    items(Todo.items, name='items', show=Todo.filter, left=0.0, top=top, width=width, height=height - top)
```

No view classes, no controller object, no lookups of a global host, cache or
focus owner from inside a view. What a view needs comes through its
parameters.

Most views are not the app's: a control or a view of a kind of value serves
every app, and is a public view of the library (section 12). The reference for
such a widget is [color_picker_view.py](../meltygui/view/color_picker_view.py):
a public view composed of small parts, each with one gesture. The app keeps its
root, its model and what only it needs. The example keeps its controls in its
own file only to show every pattern in one place.

## 2. The contract of a view

1. **Signature.** The first parameter is the view's value, under any name
   (`input_value` by convention), with the type it draws as annotation. A value
   passed by position goes to it, further ones to the next parameters, as in
   Python: `draw_thing(model, 'Title')`. Callers may pass any parameter by
   name, and none has to be passed: one without a default is built by its
   type, or is None without one (section 5). A first parameter the framework
   fills by name (rule 2, and `FILLED` in
   [gui_prototype.py](../meltygui/core/rendering/gui_prototype.py)) is not a
   value: such a view takes none.
2. **Ask the framework by name.** A parameter called `width`, `height`,
   `left`, `top`, `scroll_offset`, `draw_state`, `cache` or `event` is
   filled in by the framework when the caller does not pass it.
3. **Return `(changed, value)`**, or nothing when the view never edits (the
   caller then gets `(False, value)`).
   `changed` must be true exactly when the value differs from the input: it is
   what runs the caller again. Mutate a mutable input in place and return the
   same object; return a new value of the input's own type for an immutable one.
4. **The caller owns the value.** A view shows what its caller passes, like an
   immediate mode widget, whether or not it is cached:

   ```python
   changed, model.name = field(model.name)   # kept: the caller passes it back
   field('fixed')                            # an edit shows for a few frames, then 'fixed' again
   field()                                   # nothing passed: the view keeps its own (section 5)
   ```

## 3. Place and size

- **`left`, `top`, `width`, `height`** on the call place a view in its parent's
  content, in logical pixels. A body draws in its own space, from (0, 0) to
  (`width`, `height`); never add the view's position inside it.
- **A width or a height is one of three things**, the same on both axes:

  | Passed | Meaning |
  |---|---|
  | a number, 0 or more | that many pixels |
  | nothing, or a very large number | **fill**: from the view's left / top edge to the parent's right / bottom one. `draw_view(width=1e8, height=1e8)` says it over a size the definition fixes |
  | `None` | **wrap**: as large as the view's content |

  ```python
  @gui                              # fills its parent
  @gui(height=30)                   # fills across, 30 high
  @gui(height=None)                 # fills across, as high as its content (a paragraph)
  @gui(width=None, height=24)       # a chip: as wide as its label
  draw_view(value, width=1e8)       # fill, over a width the definition fixes
  ```

  `meltygui.FILL` (1e8) and `meltygui.WRAP` (None) are names for the same
  values: `draw_view(width=meltygui.FILL, height=meltygui.WRAP)`. They need not
  be used; use them where the call reads better with them.
- A value passed on the call applies on every run it is passed. A size the
  view always has goes on the decorator (`@gui(height=30)`).
- **Fill** follows the parent's size. The edge it starts from is `left` /
  `top` where given, else ImGui's flow. A floating window or popup has no
  parent to fill: it is 300 by 100 (`UNSIZED` in `retained_gui_prototype.py`).
- **Wrap** measures what moved ImGui's cursor: widgets, `imgui.dummy`, child
  views. Draw list shapes alone measure nothing, so reserve their space with
  `imgui.dummy(w, h)`. A wrapped width is at most the room a fill would have
  had. Inside a wrapping parent a filling view has nothing to fill and wraps
  too.
- **In the body `width` and `height` are always numbers**: the space the view
  has to draw in, whatever was asked for. Use them directly. On a wrapping
  axis they are the room the view may take, and it then becomes as large as
  what it drew. Where nothing bounds a wrapped height (a scroll view's
  content, a parent that wraps too) `height` is 8192 (`UNBOUNDED` in
  `gui_size.py`), the tallest a view can be: lay out as a scrolling view
  would.
- A body that must know what was asked takes a `canvas` parameter:
  `canvas.width` and `canvas.height` are the values passed (the call's, else
  the definition's): pixels, `1e8` for fill, None for wrap. It is read only.
  `draw_editor` uses it to be as high as its text when wrapped:

  ```python
  def draw_editor(..., width=None, height=None, canvas=None):
      if canvas.height is None:
          height = min(height, rows * line_height)     # past the room it scrolls
  ```
- A `width=0.0` or `height=0.0` in the view's own signature is not a size: it
  only asks for the resolved number.
- A size a caller computed down to nothing (`width=width - left - PAD` at 0)
  is safe: the view is drawn 1 px wide, or with no height, and returns to its
  size when room does (`_fit_size` in `retained_gui_prototype.py`). Only NaN,
  infinity or a width over 8192 raises, naming the view.
- Any number from a million up fills (`FILL_FROM` in `gui_size.py`): no view
  is that large, so a size computed that high by mistake fills its parent
  instead of raising.
- A position that is not given follows ImGui's flow. Give both in a composed
  layout.
- **`left=None` / `top=None` puts that axis in the flow**, each by itself,
  whatever position the view holds. A view that was a window, or was given a
  position once, keeps it while the call passes nothing; passing None is how
  it returns to the flow. `left` / `top` numbers stay what they were: from
  the parent's content corner, not from the flow position.
- **The root** receives `width` and `height`: the space under the window's
  title bar. `top=0` is the first pixel below the bar, so a view that fills the
  rest from `top=T` gets `height=height - T`. No app knows the bar's height.
  ImGui's own flow in the root starts there too. An absolute
  `imgui.set_cursor_pos` in the root is still in window coordinates, as is
  `left` / `top` on a `use_cache=False` view: move the flow instead
  (`imgui.set_cursor_pos((8.0, imgui.get_cursor_pos_y() + 8.0))`), or put the
  ImGui widgets in a view, where (0, 0) is the view's corner.
- Give the root a starting size and a smallest one: `width=`, `height=`,
  `min_width=`, `min_height=` on `@os_window`. The user's size is saved.
  (Hyprland does not hold a window to it: whatever size the compositor gives,
  the root is handed at least 1 by 1.)
- **A floating window** is any view called with `melty_window=True`. Its start
  is `initial={'left': 80, 'top': 120, 'width': 300, 'height': 200}`, applied
  once; after that the user's geometry wins and is saved. It has a title
  strip of its own, and its content starts below it.
- **What the call passes decides what the user can resize**, for width and
  height independently:

  | On the call | The window along that axis |
  |---|---|
  | nothing | the user's to resize (a size on the decorator is only where it starts) |
  | a number | fixed at that size |
  | `None` | always as large as its content |

  ```python
  draw_editor(text, melty_window=True, min_width=200, min_height=50)                # resizable
  draw_editor(text, melty_window=True, height=100, min_width=200)                   # 100 high, any width
  draw_editor(text, melty_window=True, height=None, min_width=200, min_height=50)   # as high as its text
  ```

  A fixed or wrapping axis is a span whose minimum and maximum are that one
  size, and nothing else: the collision rules apply to it as to any span at
  its limit (`WINDOW_COLLISION_COLUMNS.md`). A resize drag along it cannot
  change the size, so it carries the opposite edge and the window travels.
  A wrapping window is laid out in its parent's size, so it is never larger
  than that. `min_width` / `min_height` hold for it too. Passing `left` / `top`
  still pins the position. (A window that fills, `1e8`, is not built: it is
  300 by 100 and resizable.)
- A view cannot be larger than 8192 px on a side. Long content is a scroll
  view with many child views (section 8).

## 4. Identity: `name=` and `key=`

Each call site is its own view with its own state. Tell repeated calls apart:

```python
draw_float(size.x, name='x')            # a name where the views differ in meaning
draw_float(size.y, name='y')
for key, item in items.items():
    row(item, key=key)                  # a key for the items of a collection
```

- Name every call that stands under a condition, and key every call in a loop.
  An unnamed call is told apart by its order among the unnamed calls of that
  view in the parent, so a skipped call shifts the ones after it.
- A key is a string, an int, None or a tuple of those, and is stable for the
  item: its dict key, never its index in a filtered list.
- Identity never looks at the view's value.

## 5. State

Two kinds, each with one way to keep it: what a view holds is its parameters,
what the app holds is a `@save` class.

**A view's parameters are its state.** Read the parameter, write through
`draw_state`:

```python
@gui(no_save=('index',))
def options(input_value, choices=(), index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    ...
    draw_state.index = index            # kept for the next run; writing the same value costs nothing
```

- State is saved with the app's session and restored on the next launch,
  including size, position and scroll. List what must not be saved in
  `no_save=` (a popup's selection, a drag in progress, handles).
- Hover, a held button and the keyboard are not state of yours: `event` holds
  them (`event.is_hovered()`, section 6).
- A value the caller passes wins over the state on every run it is passed.
- **`draw_state` reads every parameter**, whoever supplied it:
  `draw_state.choices` or `draw_state.input_value` gives what the caller
  passed as readily as what the view keeps. The value is the one parameter a
  `return` can write; in every other way it is a parameter like the rest.
- **A passed value is read through, never copied.** `draw_state.input_value`
  goes to the caller's own value, so the state does not keep a model or a
  tensor alive, does not save it, and does not fall back to it once the
  caller passes nothing. It can be read for as long as the view is declared,
  also before its body has run; in an immediate view (`use_cache=False`) only
  while its body runs, and `AttributeError` otherwise. A cached view that is
  gone reads its own value again. A write while a value is passed
  (`draw_state.input_value = x`) is kept underneath: the read still gives the
  caller's, and the write shows once the caller passes nothing. (A passed
  keyword argument is still copied into the state and saved, as of
  2026-11-17.)
- **A parameter nobody passes needs no default.** Its annotation builds it,
  once per view, and it is state from then on, the value like any other
  parameter:

  ```python
  @gui
  def draw_thing(count: int, items: list[str], clock: Clock, note):
      ...         # 0, [], Clock() and None; the same list and the same Clock on every run

  draw_thing(name='thing')
  ```

  - A parameter without a type is None. So is one whose type builds nothing:
    `object`, `Any`, `int | None`, an abstract class (`constructor` in
    [gui_prototype.py](../meltygui/core/rendering/gui_prototype.py)).
  - **A type that needs arguments is asked for.** Where `Camera(zoom)` cannot
    be built from nothing, the view shows `draw_function(Camera)` in its own
    place (section 12): the constructor's parameters and a button. What the
    button builds is the view's state, and its body runs from then on. The
    call returns `(False, None)` until then. A caller that passes a `Camera`
    never sees the form.
  - What is built is saved with the session as far as the session can save it
    (numbers, text, lists, dicts, `DictConversion`). Any other object is built
    again at the next launch, or asked for again.
  - A type whose constructor raises fails the call, with a `TypeError` that
    names the view and the parameter.
  - Framework names (rule 2 of section 2) are the framework's to fill, not
    built.

  Worked example: [examples/gui_function.py](../examples/gui_function.py).
- **The view's value is state like any other when nothing is passed**: its
  default at first, or what its type built, then what the view wrote. A returned edit is that write,
  so a view can be prototyped with no model at all and given one later:

  ```python
  @gui
  def count(value: int = 0):
      if imgui.button(f'Count {value}'):
          return True, value + 1          # the same as draw_state.value = value + 1

  count(name='clicks')                            # the view's own count, saved with the session
  changed, Model.clicks = count(Model.clicks)     # later: the app's; the edit is the caller's to keep
  ```

  A value annotated with a `DictConversion` class is injected the same way
  when nothing is passed (`def picker(selection: Selection = None)`). An edit
  to a value the caller passed is the caller's to keep: `draw_state` reads it
  until the caller has answered, and the view does not own or save it. Worked example: [examples/gui_value_name.py](../examples/gui_value_name.py).
- No ad hoc fields on `draw_state`: only declared parameters.
- This is state of one view: its size, its scroll, its selection. What the
  app is about (its documents, its layout, its settings) is the app's.

**The app's state is a `@save` class.** The class is the state: there is no
instance, its attributes are the fields, and the values in its body are what
a first launch starts from.

```python
from meltygui import os_window, save


@save
class Todo:
    items = {'1': {'title': 'Write the guide', 'done': True}}
    filter = 'All'
    next = 2


@os_window(name='To do', app_id='meltygui-todo')
def app(width=None, height=None, cache=None):
    picked, Todo.filter = dropdown(Todo.filter, name='filter', choices=FILTERS)
    items(Todo.items, name='items', show=Todo.filter)
```

- It is saved when the app closes, in a pickle of its own beside the module
  that defines the class, named after the class: `Todo.pkl` next to
  `gui_todo.py`. One store, one file, next to the code it belongs to.
  `@save('items.pkl')` names the file; an absolute path puts it elsewhere.
  Nothing else to name: no `app_id`, no base class, no call.
- It is read where the class is decorated, so the saved values are there for
  every line below the class. What its values are instances of is defined
  above it.
- The public plain attributes are saved. A name that starts with `_`, a
  method and a property are not. A class in the body is saved with it
  (`Look.Editor.tab_width`).
- A value edited in place is saved as it is: `draw_tiles(Tiles.tree)` edits
  the dict and the layout comes back, sizes included.
- The root passes the parts on, like any model; a view does not reach for
  the class itself.
- A field added later starts from its default; one that is removed is dropped.
  A pickle that does not load is moved aside (`Todo.pkl.broken-<time>`) and
  the app starts from the defaults.
- **`@save(to_code=True)`** keeps the class in its own source instead: on
  close, each value that changed is written over its default in the file.
  For what belongs in the code, such as a window's start size or a font size
  the developer tunes while the app runs. The values are literals (numbers,
  texts, `True`, `False`, `None`, lists, tuples, dicts, sets), each assigned
  once at the top of the class. A list or dict too long for its line is
  written over indented lines, a tree one level per indent (`wrap` in
  [tree_model.py](../meltygui/model/tree_model.py); levers `WIDTH`, `TAB`).
  A default that is an expression (`2 / 3`) or
  that was edited in the file while the app ran is left as it is, and stderr
  says so.
- This is the one way an app keeps state. `meltygui.persisted(...)` and a
  `DictConversion` held as the app's model are how older apps did it: they
  still work, new code uses neither.

How it works, and its levers: [app_save.py](../meltygui/core/runtime/app_save.py).
(Run on 2026-11-14, each from a copy in a temp folder: the example and
`~/Desktop/tile-view` over a relaunch, and a `to_code` class written back on
close.)

## 6. Input

A view that takes input has an `event` parameter and asks it what happened:

```python
@gui(cursor=mouse_cursor.HAND, height=TOOLS)
def button(input_value: str, width=0.0, height=0.0, event=None):
    pressed = event.left_mouse_down()
    face = HOT if event.is_hovered() else FACE
    ...                                             # draw the box in `face`
    return pressed, input_value
```

How to read `event`:

- **A name that starts with `is_` is a state.** It holds until it changes:
  `is_hovered()`, `is_focused()`, `is_left_mouse_held()`.
- **Every other method says what happened since the view last ran.** It is
  true in the run that follows and false again in the next:
  `left_mouse_down()`, `left_mouse_clicked()`, `mouse_moved()`.
- **The rest are plain attributes**: `x`, `y`, `dx`, `dy`, `total_dx`,
  `total_dy`, `clicks`, `ctrl`, `shift`, `alt`.

| Ask | It says |
| --- | --- |
| `left_mouse_down()` | The left button went down over the view. |
| `left_mouse_up()` | It was released, wherever the pointer is by then. |
| `left_mouse_clicked()` | It was released where it was pressed: a click, told on the release. |
| `left_mouse_double_clicked()` | It went down for the second time in a row, told on that press. `clicks` counts on to 3. |
| `left_mouse_drag()` | The pointer moved with the button held. `x`, `y`: where it is, also outside the view. `dx`, `dy`: how far it went since the last run. `total_dx`, `total_dy`: since the press. |
| `left_mouse_held_outside()` | Once a frame while the button is held and the pointer rests outside the view: a selection dragged past an edge keeps scrolling. |
| `is_left_mouse_held()` | The button is down since a press over the view. |
| `right_mouse_...`, `middle_mouse_...` | The same seven for the other buttons. |
| `is_hovered()` | The pointer is over the view. The view runs when that begins and ends, not for the moves between. |
| `mouse_moved()` | The pointer moved over the view with no button held; `x`, `y` say where. The view runs for every such move: for a list that selects the row under the pointer. |
| `mouse_wheel()`, `mouse_wheel_horizontal()` | Wheel steps no scroll view took, as a number (0.0: none). |
| `scrolled()` | A scroll view under the pointer, this one or one inside it, was scrolled by the wheel. |
| `keys()` | The key presses and repeats in order, as `(key, mods)`, for the view with the keyboard. `key` is a `KEY_` constant of `meltygui.core.windowing.window_constants`. |
| `key_down(key)` | Whether that key is among them. |
| `text()` | What was typed, as one string. |
| `is_focused()`, `focus_gained()`, `focus_lost()` | The view has the keyboard, was given it, lost it. |
| `x`, `y` | The pointer in the view's own coordinates, as of the last button, drag or hover the view was told. |
| `ctrl`, `shift`, `alt` | The modifiers held then. |

The vocabulary and its exact rules are the text of
[event.py](../meltygui/core/input/event.py); who is routed what is
[gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py).

**Asking is subscribing.** There is no list of events beside the body. A view
runs when something its body has asked about happens, and for nothing else:
the button above runs for a left press or release and when the pointer enters
or leaves, never for a move inside it or a key.

- **Ask at the top of the body, on every run**: before any `return`, and not
  behind a condition that only input can make true. What was never asked is
  never routed. Keys most of all: a left press gives the keyboard to the view
  under it that had asked for keys before that press, so a field that asks
  only once it is open never gets them (`number` in
  [value_view.py](../meltygui/view/value_view.py) asks first, then decides).
- **A view that only paints is `listen=()`**: it runs when its arguments
  change and never for input. A view that has neither `event` nor `listen=()`
  is run by raw pointer samples, which is what an ImGui widget in it needs
  (section 9); give every view of your own one or the other.
- **A view with no `event` pays nothing for the ones that have it.** With no
  view in the app that takes input a frame pays one dictionary truth test.
- The root under `use_cache=False` has no `event`: input belongs to views.
- **`cursor=`** on the decorator sets the pointer's shape over the view
  (`mouse_cursor.HAND`, `TEXT`, `RESIZE_EW` from `meltygui.core.input`).
- **The keyboard** goes to a view by a left press on it, or by
  `cache.focus.take(cache.current)`; a popup that asks for keys takes it while
  it is open (section 7).

**The window's gestures are not yours.** A left drag moves the window the view
is in, a right drag resizes it.

- Asking about the left button takes the left drag away over the whole view,
  so ask only when the view has a left gesture of its own. When that covers
  less than its box say where, on every run:
  `event.reach(left, top, right, bottom)` in the view's coordinates (the chip
  of `draw_bool`). The rest of the box still moves the window.
- A container never asks about a button to catch presses on its rows: the
  rows do.
- Nothing takes the right drag. An action on the right button is
  `event.right_mouse_clicked()`, on its release. A view that asks only about
  the right button leaves the left drag to the window. A menu of options on
  the right click is `@context_menu` (section 7).

**ImGui widgets.** Views that ship in `meltygui/` use none: they draw with
the draw list and take input from `event`, and new views should do the same.
In an app's own view `imgui.button`, `checkbox`, `slider_float` and
`input_text` are supported, also when the view is cached (section 9). Not
usable yet inside a cached view: `imgui.combo`, ImGui popups and tooltips
(bug `c3582148b768`) and `begin_child` (bug `fb94b9e2eed2`, it can abort the
app). A dropdown is section 7, a scrolling area section 8.

## 7. Popups

A list under a button, a picker, a menu: an ordinary view called with
`popup=<open event>`. Nobody keeps an open flag.

```python
@gui(cursor=mouse_cursor.HAND, height=TOOLS)
def dropdown(input_value, choices=(), width=0.0, height=0.0, event=None):
    pressed = event.left_mouse_down()
    face = HOT if event.is_hovered() else FACE
    picked, value = options(input_value, name='options', popup=pressed, choices=choices,
                            left=0.0, top=height + 2.0, width=width, height=len(choices) * ROW)
    ...                                             # draw the box in `face`, and `value`
    return picked, value
```

- `popup=` takes an event (true on the run of the press), not a flag that
  stays true. `left` / `top` are in the caller's coordinates.
- The popup view is any `@gui` view. It closes itself after a pick with
  `cache.focus.close()` and returns `(True, choice)` from that run.
- A press outside, Escape and a second press on the anchor close it; the
  framework does that, and keeps it on screen.
- A popup whose body asks for keys (`event.keys()`, `event.key_down(...)`)
  has the keyboard while it is open and gives it back as it closes. One that
  asks for none leaves the keyboard where it is.
- Per opening state is a `no_save` parameter: it is its default again on each
  opening (`index` in the example's `options`).
- **A submenu is a popup the list calls for its selected row**, keyed by the
  row: `options(value, key=text, popup=opened, choices=choice, left=width,
  top=index * ROW, ...)`. A pick in it comes back as the call's return; when
  another row is selected the call is no longer made and the submenu closes.
  `options` in [choice_view.py](../meltygui/view/choice_view.py) is the
  worked example.
- Until `9ed33b19b8e3` is fixed, draw the anchor after the popup call, from the
  value it returned (section 14).

Rules and nesting: [Temporary popups](GUI_PROTOTYPE.md#temporary-popups).

**A context menu** is `@context_menu({label: function})` above `@gui`. Nothing
else is written: no event, no popup call, no open flag.

```python
from meltygui import context_menu, gui

def delete(input_value, items):         # the note's key, and the dict its list passed it
    del items[input_value]

@context_menu({'Delete': delete, 'Say hello': lambda: print('hello')})
@gui(height=ROW)
def note(input_value: str, items: dict = None, width=0.0, height=0.0):
    ...

@context_menu({'Add a note': add})
@gui(scroll=True)
def notes(input_value: dict, width=0.0, height=0.0):
    for at, key in enumerate(input_value):
        note(key, key=key, items=input_value, left=0.0, top=at * ROW, width=width, height=ROW)
```

- A right click shows the options of the view under the pointer, then those of
  each view around it, innermost first, a line between two views' options: a
  note's `Delete`, then the list's `Add a note`. So give each thing that has
  options of its own a view of its own.
- **An option's function is given what it names** of its view's parameters:
  the value (`input_value`, also when the caller passed it), any other
  parameter (`items`), `draw_state`, `cache`. One that takes nothing is just
  called. A name the view does not have keeps the function's default, and is
  an error without one. To reach something of the view around it, pass it
  down as an argument (`items=` above).
- **A dict of options in place of a function is a submenu**, to any depth:
  `{'Delete': delete, 'Move': {'Up': up, 'Down': down}}`. Its row has a
  chevron; the pointer, Right or Enter opens it beside the row, Left closes
  it. Its functions are given what they name like any option's.
- After a pick the option's view and the views around it run again, so an
  option may change the model in place. Keep the dict of options a module
  constant, as written above the view.
- It opens on the release of the button: a right drag still resizes. The
  pointer or the arrows and Enter pick; a press outside or Escape closes it.
- Of a view that uses the right click itself (`event.right_mouse_clicked()`)
  and a view with a menu, the one nearer to the pointer takes the click.
- A view without a menu costs nothing for the ones that have one.
- An immediate view (`use_cache=False`) has no menu, except the root of a
  window: its options are the last section. Its functions get `cache` only.

Worked example: [examples/gui_context_menu.py](../examples/gui_context_menu.py).
How it works: [Context menus](GUI_PROTOTYPE.md#context-menus).

## 8. Lists and scrolling

```python
@gui(scroll=True)
def items(input_value: dict, show='All', width=0.0, height=0.0):
    shown = [key for key, item in input_value.items() if ...]
    changed = False
    for at, key in enumerate(shown):
        edited, _ = row(input_value[key], key=key, left=0.0, top=at * ROW, width=width, height=ROW)
        if edited:
            changed = True
    return changed, input_value
```

- `scroll=True` makes the view a viewport. Its children are placed in content
  coordinates (`top=at * ROW`); the wheel scrolls it and the offset is saved.
- One child view per thing that changes alone. Measured on the example: a
  scroll runs no body except rows shown for the first time, an added item runs
  the list and the new row, a keystroke runs its row and the list.
- What the viewport's own body draws does not scroll. Scrolling content is
  child views.
- Thousands of rows: declare only the rows near the viewport (read
  `scroll_offset`, and ask `event.scrolled()` so that a scroll runs the view),
  as `draw_collection` does ([dict_view.py](../meltygui/view/dict_view.py));
  the frame then does not depend on the size of the data.

## 9. What runs a view

A cached view's body runs when one of these happens, and otherwise its last
output is shown again:

| Cause | Detail |
| --- | --- |
| An argument changed | Numbers, strings and bools compare by value. Every other object compares by identity. |
| A child returned `changed` | The parent runs to receive it and returns it upward. |
| Its state was written with another value | `draw_state.name = value`. |
| Something its body asked `event` about happened | Section 6. |
| `cache.invalidate(obj)` | For an object changed in place by code outside the views that show it. |
| `cache.wake(node)` | From any thread (section 11). |
| A drag of its dividers ended | Only for a view that asked a `columns` / `rows` layout for its `sizes()`, and only if the drag left other sizes. |

What follows from the first row:

- **A tuple, list, dict, lambda or bound method built in the caller's body is
  a changed argument on every run.** Keep such arguments as module constants
  or state (`FILTERS` in the example).
- **An object edited in place is not a changed argument.** When a view made
  the edit, its `(True, obj)` return is enough. When other code did (the root
  after a button press, a model's method), call `cache.invalidate(obj)`:

  ```python
  if added:
      Todo.items[key] = {...}
      cache.invalidate(Todo.items)      # `cache` is a parameter of the root
  ```

- Never repair a stale view with `use_cache=False`, `live=True` or a state
  write on every run. Find the missing cause above.
- **A divider drag runs the views it resizes, not the view that declared the
  layout.** A view that keeps the sizes of its cells in its value asks for
  them, `layout.sizes()`, and runs once when the drag ends: `place` in
  [tiles_view.py](../meltygui/view/tiles_view.py). Drawing of your own inside
  a cell (anything but child views and nested layouts) makes the layout one
  that is recaptured on every frame of a drag, and its owner runs each time.
- The root in the example is `use_cache=False`: it runs on every frame the
  window draws, so it may read the model directly. That is for the root only.

**ImGui widgets keep their view running.** A cached view that holds an ImGui
item runs when the pointer enters and leaves it, on a press, on every frame
while a button or key is held on an active item or while a text field has the
keyboard, and on the release. It gets no run on the frame after the release;
a view that needs one asks with `cache.wake_next(cache.current)` (not run
here). The mechanism is `ImGuiInput.process` and `finish` in
[gui_imgui_input.py](../meltygui/core/rendering/gui_imgui_input.py).

## 10. Drawing

```python
from meltygui.hdr_color import pack_color

FACE = pack_color(0.20, 0.24, 0.30)                                  # r, g, b[, a]
TEXT = pack_color(0.92, 0.94, 0.96)

draw_list = imgui.get_window_draw_list()
draw_list.add_rect_filled(0.0, 0.0, width, height, FACE, 5.0)        # x0, y0, x1, y1, colour, radius
draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * 0.5, TEXT, label)
```

- Also: `add_rect(x0, y0, x1, y1, colour, radius, 0, thickness)`,
  `push_clip_rect(x0, y0, x1, y1, True)` / `pop_clip_rect()` around text that
  may overflow, `imgui.calc_text_size(text).x` to measure.
- A view's background is `tint=(r, g, b, a)` on the decorator or the call.
- Icons are Font Awesome glyphs written as literals in f-strings.
- Colours and sizes are constants at the top of the file, each with a comment
  saying what it changes. A family of views that should follow a tint builds
  its colours from it (`look` in [value_view.py](../meltygui/view/value_view.py)).

## 11. Threads and slow work

Nothing slow in a body: the budget is 8.33 ms for the whole frame. Work goes
to a thread that writes plain fields and then runs the view that shows them:

```python
@gui(listen=(), no_save=('started',))
def uptime(input_value: Clock, started=False, draw_state=None, cache=None):
    if not started:
        node = cache.current
        input_value.start(lambda: cache.wake(node))     # the thread calls this after each result
        draw_state.started = True
```

- `cache.wake(node)` is the only thing a thread may call on `cache`.
- A model read by several views wakes its readers itself with `Readers`
  ([GUI_PROTOTYPE.md](GUI_PROTOTYPE.md#results-from-other-threads); not run
  here).
- What a feature starts (a thread, a process) it also stops, at its own
  action and at exit.
- **A GPU resource is owned by the view that draws it.** Declare a parameter
  typed with a small class that holds it (`picture: Picture`, built once per
  view, section 5), list it in `no_save=`, and end it with
  `cache.on_retire(cache.current, picture.close)`: the callback runs on the
  render thread when the view is retired or the window closes. `field` in
  [color_picker_view.py](../meltygui/view/color_picker_view.py) keeps an fp16
  texture this way and bakes it again only when its hue changes.

## 12. Public views: publish what you build, use what exists

Most views are public. Unless a view only makes sense inside one app, it is
part of the library, and every app gets it from one namespace:

```python
import meltygui

changed, model.gain = meltygui.rust.draw_float(model.gain, name='gain', left=8.0, top=8.0, width=160.0)
```

- **Name**: `draw_<thing>` (`draw_bool`, `draw_collection`). A helper view
  that only serves one public view keeps a short plain name in that view's
  file (`branch` and `end` in [dict_view.py](../meltygui/view/dict_view.py)).
- **Home**: `meltygui/view/<feature>_view.py`, beside the views of the same
  feature.
- **Publish**: run `.venv/bin/python stubs.py`. It finds every module-level
  `@gui` function named `draw_<thing>`, rewrites `VIEWS` in
  [rust.py](../meltygui/rust.py) and the stubs the IDE completes from.
  Nothing is imported until the name is first used.
- **Use**: an app writes `meltygui.rust.draw_thing(...)` or
  `from meltygui.rust import draw_thing`. Library code imports the defining
  module.
- **A view's file imports through the package** (`from meltygui.view.value_view
  import look`), never a neighbour by its bare file name (`from look import
  PAD`). A bare import only resolves when the script's own folder is on the
  path, so nobody else can import the view.

What is there already:

| Need | Use |
| --- | --- |
| A bool, number or line of text to edit | `draw_bool`, `draw_int`, `draw_float`, `draw_str` from `meltygui.rust` |
| Any value, nested dicts and lists, dataclasses | `meltygui.rust.draw_any(value, name=...)`; a view of your own for a type, a key name or a shape is `@is_default_for(...)` above `@gui` |
| Code or long text | `draw_editor`, from `meltygui.view.editor_view` for now ([NATIVE_EDITOR.md](NATIVE_EDITOR.md#using-draw_editor)) |
| A button | `draw_button` from `meltygui.rust`: `pressed, _ = draw_button('Run', name='run')` |
| A function to call with arguments the user edits | `ran, _ = draw_function(fit, name='fit')` from `meltygui.rust`: its parameters as values (`draw_any` of a dict the view keeps, each starting at its default or what its type builds) and a button that calls it, in that run, so only for a call that returns at once. `ran` is true in the run of a call that returned; what it returned is the view's `result` (`ran, _, state = draw_function(fit, return_extras=True)`, then `state.result`); an exception is shown under the button. A class is called the same way and builds an instance ([function_view.py](../meltygui/view/function_view.py)) |
| One of a few choices | `draw_dropdown(value, choices=CHOICES)` from `meltygui.rust`. `CHOICES` is a sequence of the choices (`label=` names those that are not text) or a dict `{label: choice}`; in a dict a value that is a dict is a submenu, to any depth, and the value is the leaf picked ([choice_view.py](../meltygui/view/choice_view.py)) |
| Tiles the user splits and joins from their corners, as in Blender | `draw_tiles(tree, name='tiles', views=VIEWS, width=width, height=height)` from `meltygui.rust`; `VIEWS` is a module tuple of `@gui` views of a tile's dict, and each tile picks one from a dropdown in its head; the tree is plain dicts and may start as `{}` or bare nested `{'cells': [{}, {}]}` (ids, axes and sizes are filled in); a divider drag runs only the resized tiles, and `draw_tiles` once at its end to write the sizes; no cell goes around it: where it ends on the window's edge a divider pushed against that edge resizes the window, and given a `width` / `height` elsewhere it keeps them and the push moves the window's left / top edge; `width=None` / `height=None` wraps it to its tiles' minimums ([tiles_view.py](../meltygui/view/tiles_view.py)) |
| A colour to edit | `changed, tint = draw_color(tint, name='tint', width=60.0)` from `meltygui.rust`: a chip, and a press opens the picker under it. `draw_color_picker(tint, ...)` is the picker by itself, sized by `extent(len(tint))` of its file. A colour is an extended sRGB `(r, g, b)` or `(r, g, b, a)`, tuple or list: the picker covers Display P3 and, above white, up to `nits=` (10,000) ([color_picker_view.py](../meltygui/view/color_picker_view.py), [examples/gui_color.py](../examples/gui_color.py)) |
| A popup of your own, a picker | Section 7; [examples/gui_popups.py](../examples/gui_popups.py) |
| A right click menu on a view | `@context_menu({label: function})` from `meltygui`, above `@gui` (section 7) |
| A whole standalone ImGui app as one view | `gui_module('app.py')` ([gui_module.py](../meltygui/core/rendering/gui_module.py)) |

## 13. Style and finishing

- Beautiful, concise, fast, self explanatory. One clean term per name.
- **Not terse.** Concise means no wasted code, not the fewest characters. One
  step per line, each with a name that says what it is. No `^=` or `!=` used
  as logic on booleans, no chained conditional expressions, no several
  assignments packed into one tuple line, no conditional expression used as a
  statement. Write `if pressed: on = not on`, not `on = value != flipped`.
- **Floats are written `0.0`, `8.0`, `0.5`**, never `0.`, `8.` or `.5`.
- **No trailing commas** after the last argument or element.
- A module docstring that says what the file is, how to run it and where its
  parts are. Constants at the top as the levers. Comments say how to change
  the code, not what the next line does.
- No U+00B7 in UI text or comments.
- One file per feature; a second file when a second user appears.
- A framework gap goes to the bug tracker (project `meltygui`) with steps and
  the code that shows it. Say so in your report.

**Check it before reporting.** Run the app on a reserved agent desktop with
an `app_id` of its own, and look at screenshots:

1. It draws, and fills the window to the bottom edge.
2. Every control does its job, and what it shows afterwards is the new value.
3. Idle costs nothing: with a `print` at the top of each body, no view runs
   while nothing happens, and an edit runs only the views it concerns.
4. A left drag on empty space of the view moves the window it is in. An OS
   window whose root has no `columns` / `rows` layout only moves by its title
   bar for now (bug `78d4ccd04ec6`), so test this with the view in a floating
   window (`melty_window=True`): drag its empty space and compare two
   screenshots.
5. Close it and start it again: state that should persist did.
6. A public view imports from anywhere: from another folder,
   `python -c "from meltygui.rust import draw_thing"` succeeds.

Report what you saw in the screenshots, which backend it ran on, and what you
did not check.

## 14. Known traps

| Trap | Until fixed |
| --- | --- |
| `imgui.combo` in a cached view: list misplaced, no row can be picked (`c3582148b768`) | A `popup=` view |
| ImGui popups, tooltips, `begin_child` in a cached view (`fb94b9e2eed2`, `689e8767ed78`) | `popup=`, `scroll=True` |
| The view that opened a popup is not run again with the pick (`9ed33b19b8e3`) | Draw after the popup call, from its return |
| A view is not run again when its caller passes back the edit it returned: an ImGui button or checkbox that drew before the edit stays stale until the pointer moves (`90293f1fa9dd`). A view that was passed nothing is run again | Draw after handling the input, or `cache.wake_next(cache.current)` in the run that returns the edit |
| A pick in a popup opened from a popup (a submenu) runs the outer popup and its caller twice in that frame (`1bd0b2d648ed`) | Nothing: the value is right, the double render mark shows for a second |
| A left drag on an OS window's body does not move it unless the root holds a `columns` / `rows` layout (`78d4ccd04ec6`) | Drag the title bar |
| A view larger than 8192 px raises (`c3b99fd82886`) | Child views in a scroll view |
| The wheel carries no modifiers and a scroll view takes it first (`c1cc6c3481b5`) | No Ctrl+wheel zoom yet |
| An exception in a body can end as a C++ abort with no traceback (`fb94b9e2eed2`) | Run with `-X faulthandler`; wrap the suspect call in `try` to print it |
| In a venv Hub built (uv's Python has no `os.memfd_create`) no pointer shape can be set, and the app can exit when the pointer comes back into the window after leaving over a `cursor=` view (`a6e9307b63b6`) | Keep `cursor=`; test with `~/Desktop/meltygui/.venv/bin/python` |
| A scroll view keeps its offset when it grows or its content shrinks: blank space below the last row (`f41b29983344`) | Clamp `scroll_offset` in the view and write it back (`rows` in the gallery's `finder.py` does) |
