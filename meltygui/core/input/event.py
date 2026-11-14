"""What a `@gui` view is told about input: its `event` parameter.

    @gui(cursor=mouse_cursor.HAND, height=30)
    def toggle(input_value: bool, width=0.0, height=0.0, event=None):
        flipped = event.left_mouse_down()
        if flipped:
            input_value = not input_value
        face = HOT if event.is_hovered() else FACE
        ...
        return flipped, input_value

A view that has an `event` parameter gets one `Event` of its own, the same object on every run.
How to read it:

- A name that starts with `is_` is a state: it holds until it changes (`is_hovered`,
  `is_focused`, `is_left_mouse_held`).
- Every other method says what happened since the view last ran: true in the run that follows
  it, false again in the next (`left_mouse_down`, `left_mouse_clicked`, `mouse_moved`).
- `x`, `y`, `dx`, `dy`, `total_dx`, `total_dy`, `clicks`, `ctrl`, `shift` and `alt` are plain
  attributes: where the pointer was and what was held when the buttons and the hover last
  spoke.

Asking is subscribing. There is no list of events to keep beside the body: the first time a body
asks `event.right_mouse_clicked()` the view is routed the right button, and from then on it runs
when that button does something over it. What a body never asks about never runs it. So ask at
the top of the body, before any early return, and a view that takes no `event` costs nothing.

Asking about a button is also what takes that button from the window: a left drag moves the
window unless the view under the pointer asked about the left button there (`reach` narrows
"there"). Nothing a view asks takes the right drag away; a right click is `right_mouse_clicked`,
on the release.

Who routes what, and when a view runs for it, is core/rendering/gui_imgui_input.py. The keyboard
and the popups are core/input/focus.py. To add a kind of event: a bit below, a field in
`__init__` and `clear`, a method here, and the lines in `ImGuiInput.deliver` that write it.
"""

# What a body asked about, as bits of `Event.asked`. A button's bit is `1 << button`
# (0 left, 1 right, 2 middle), which is what the router computes.
LEFT = 1
RIGHT = 2
MIDDLE = 4
OUTSIDE = 8                      # the held button resting outside the view
HOVER = 16                       # the pointer entering and leaving
MOVE = 32                        # every move of the pointer over the view
WHEEL = 64
SCROLL = 128
KEYS = 256                       # key presses and typed text: the view can have the keyboard
FOCUS = 512
TOUCH = 1024
# Nothing happened: shared by every Event, never appended to.
NONE = ()


class Event:
    __slots__ = ('asked', 'area', 'x', 'y', 'dx', 'dy', 'total_dx', 'total_dy', 'clicks', 'ctrl', 'shift', 'alt',
                 'over', 'keyboard', 'held', 'start_x', 'start_y', 'travel',
                 'downs', 'ups', 'taps', 'drags', 'rests', 'moves', 'wheel_x', 'wheel_y', 'scrolls',
                 'pressed', 'typed', 'gained', 'lost', 'contacts')

    def __init__(self):
        self.asked = 0              # the bits above this view's body asked about, on any run
        self.area = None            # (left, top, right, bottom) the view takes buttons in (`reach`), None: its box
        # The pointer in the view's own coordinates, as of the last button, drag or hover it was told.
        self.x = 0.0
        self.y = 0.0
        # How far the pointer went with a button held: since the last run, and since the press.
        self.dx = 0.0
        self.dy = 0.0
        self.total_dx = 0.0
        self.total_dy = 0.0
        self.clicks = 0             # 1, 2 or 3: which press in a row the last one was
        self.ctrl = False
        self.shift = False
        self.alt = False
        # States.
        self.over = False           # the pointer is over the view
        self.keyboard = False       # the view has the keyboard
        self.held = 0               # buttons down since a press over the view, as bits
        self.start_x = 0.0          # where the last press was
        self.start_y = 0.0
        self.travel = 0.0           # the furthest the pointer went from there: a click stays within CLICK_PIXELS
        # What happened since the last run (`clear`). Buttons as bits.
        self.downs = 0
        self.ups = 0
        self.taps = 0
        self.drags = 0
        self.rests = 0
        self.moves = False
        self.wheel_x = 0.0
        self.wheel_y = 0.0
        self.scrolls = False
        self.pressed = NONE         # [(key, mods)]
        self.typed = ''
        self.gained = False
        self.lost = False
        self.contacts = NONE        # [Touch]

    def clear(self):
        """Forget what happened; the states stay. Called after each run that was told something
        (executor.rs), not by views."""
        self.downs = 0
        self.ups = 0
        self.taps = 0
        self.drags = 0
        self.rests = 0
        self.dx = 0.0
        self.dy = 0.0
        self.moves = False
        self.wheel_x = 0.0
        self.wheel_y = 0.0
        self.scrolls = False
        self.pressed = NONE
        self.typed = ''
        self.gained = False
        self.lost = False
        self.contacts = NONE

    def reach(self, left, top, right, bottom):
        """Take the buttons only inside this part of the view's box, in its own coordinates. The
        rest of the box stays the window's: a left drag there moves it. Call it on every run the
        part may have changed; a part without width takes nothing."""
        self.area = (left, top, right, bottom)

    # --- buttons: left, right and middle read alike ----------------------------
    def left_mouse_down(self):
        """The left button went down over the view."""
        self.asked |= LEFT
        return self.downs & LEFT != 0

    def right_mouse_down(self):
        self.asked |= RIGHT
        return self.downs & RIGHT != 0

    def middle_mouse_down(self):
        self.asked |= MIDDLE
        return self.downs & MIDDLE != 0

    def left_mouse_up(self):
        """The left button was released, wherever the pointer is by then, after a press over the view."""
        self.asked |= LEFT
        return self.ups & LEFT != 0

    def right_mouse_up(self):
        self.asked |= RIGHT
        return self.ups & RIGHT != 0

    def middle_mouse_up(self):
        self.asked |= MIDDLE
        return self.ups & MIDDLE != 0

    def left_mouse_clicked(self):
        """The left button was released where it was pressed: a click, told on the release.
        (How far "where" reaches is CLICK_PIXELS in gui_imgui_input.py.)"""
        self.asked |= LEFT
        return self.taps & LEFT != 0

    def right_mouse_clicked(self):
        """A right click. A right press may be the start of a window resize, so act on this."""
        self.asked |= RIGHT
        return self.taps & RIGHT != 0

    def middle_mouse_clicked(self):
        self.asked |= MIDDLE
        return self.taps & MIDDLE != 0

    def left_mouse_double_clicked(self):
        """The left button went down for the second time in a row, told on that press.
        `clicks` counts on to 3, for a triple click."""
        self.asked |= LEFT
        return self.downs & LEFT != 0 and self.clicks == 2

    def right_mouse_double_clicked(self):
        self.asked |= RIGHT
        return self.downs & RIGHT != 0 and self.clicks == 2

    def middle_mouse_double_clicked(self):
        self.asked |= MIDDLE
        return self.downs & MIDDLE != 0 and self.clicks == 2

    def left_mouse_drag(self):
        """The pointer moved with the left button held, after a press over the view: `x` and `y`
        say where it is (also outside the view), `dx` and `dy` how far it went since the last run,
        `total_dx` and `total_dy` since the press."""
        self.asked |= LEFT
        return self.drags & LEFT != 0

    def right_mouse_drag(self):
        """As `left_mouse_drag`. The window around the view is resized by the same drag."""
        self.asked |= RIGHT
        return self.drags & RIGHT != 0

    def middle_mouse_drag(self):
        self.asked |= MIDDLE
        return self.drags & MIDDLE != 0

    def left_mouse_held_outside(self):
        """Once a frame while the left button is held and the pointer rests outside the view:
        what a selection dragged past an edge needs to keep scrolling. The view runs every frame
        for as long as that lasts."""
        self.asked |= LEFT | OUTSIDE
        return self.rests & LEFT != 0

    def right_mouse_held_outside(self):
        self.asked |= RIGHT | OUTSIDE
        return self.rests & RIGHT != 0

    def middle_mouse_held_outside(self):
        self.asked |= MIDDLE | OUTSIDE
        return self.rests & MIDDLE != 0

    def is_left_mouse_held(self):
        """The left button is down since a press over the view."""
        self.asked |= LEFT
        return self.held & LEFT != 0

    def is_right_mouse_held(self):
        self.asked |= RIGHT
        return self.held & RIGHT != 0

    def is_middle_mouse_held(self):
        self.asked |= MIDDLE
        return self.held & MIDDLE != 0

    # --- the pointer without a button ------------------------------------------
    def is_hovered(self):
        """The pointer is over the view (the innermost one around it that asks). The view runs
        when that begins and ends, not for the moves between."""
        self.asked |= HOVER
        return self.over

    def mouse_moved(self):
        """The pointer moved over the view with no button held; `x` and `y` say where. The view
        runs for every such move: ask this only to follow the pointer (the row under it)."""
        self.asked |= MOVE
        return self.moves

    def mouse_wheel(self):
        """Steps the wheel turned over the view that no scroll view took, away from the user
        counting up. 0.0 when it did not turn."""
        self.asked |= WHEEL
        return self.wheel_y

    def mouse_wheel_horizontal(self):
        """Sideways wheel steps, as `mouse_wheel`."""
        self.asked |= WHEEL
        return self.wheel_x

    def scrolled(self):
        """A scroll view under the pointer, this one or one inside it, was scrolled by the wheel."""
        self.asked |= SCROLL
        return self.scrolls

    def touches(self):
        """The touch contacts over the view as `Touch(kind, contact, x, y)`, from hosts that report them."""
        self.asked |= TOUCH
        return self.contacts

    # --- the keyboard ----------------------------------------------------------
    def keys(self):
        """The key presses and repeats, in order, as `(key, mods)`: `key` is a `KEY_` constant of
        core/windowing/window_constants.py, `mods` the modifier bits held with it. They come to
        the view that has the keyboard; a left press on a view that asks gives it the keyboard."""
        self.asked |= KEYS
        return self.pressed

    def key_down(self, key):
        """Whether `key` was pressed or repeated, with any modifiers."""
        self.asked |= KEYS
        for pressed, mods in self.pressed:
            if pressed == key:
                return True
        return False

    def text(self):
        """What was typed, as one string; '' when nothing was. A printable key is in `keys()` as well."""
        self.asked |= KEYS
        return self.typed

    def is_focused(self):
        """The view has the keyboard."""
        self.asked |= FOCUS
        return self.keyboard

    def focus_gained(self):
        """The view was given the keyboard. (A view that takes it itself, with `cache.focus.take`,
        reads `is_focused()` after that.)"""
        self.asked |= FOCUS
        return self.gained

    def focus_lost(self):
        """The keyboard went to another view, or to none."""
        self.asked |= FOCUS
        return self.lost


# What a view declared with `listen=()` has: nothing asked, so nothing runs it. Shared, never written.
QUIET = Event()
