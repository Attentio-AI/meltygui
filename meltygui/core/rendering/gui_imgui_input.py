"""Retained-view invalidation using the one host ImGui context's input.

ImGui owns widget IDs, keyboard focus and editing state globally. This bridge
tracks which retained views must execute; it never copies IO into view contexts.

A view whose body takes ``event`` is event driven instead: raw pointer and key
samples never rerun it. It runs when something it asked its ``Event`` about
happened, and reads it there (core/input/event.py has the vocabulary; asking is
subscribing). This file is the router: `deliver` turns each frame's sample into
what the views' events say and wakes the views that asked.

- A press of a button goes to the innermost view around the hit one that asked
  about that button (`taker`), inside what it takes of its box (`Event.reach`).
  That view holds the pointer until every button is up: drags and releases are
  its own, wherever the pointer goes.
- A left press gives the keyboard to the innermost view around the hit one that
  asked for keys, so popovers and inline widgets leave the keyboard with their
  editor. Who has the keyboard, and the temporary popups a press or Escape
  closes, are `cache.focus` (core/input/focus.py).
- Hover goes to the innermost view around the pointer that asked about it, and
  is kept while a button is held.
- The wheel goes to the innermost asking view when no scroll view took it.

A view declared ``@gui(listen=())`` only paints: nothing runs it but its
arguments. Neither kind costs anything in a frame: with none of them in the
app a frame pays one dictionary truth test.

Window gestures are the host's by default, over every view: a left drag moves
the Melty or OS window, a right drag resizes it. A view overrides the left
one by asking about the left button (``left``: text selection, a list's rows),
over its whole rectangle or over the part of it the view says it uses
(``Event.reach``: an editor's rows, so the empty space below them still moves
the window); nothing a view asks overrides the right one. An ImGui item still
owns the button that activated it (``finish``).

``@gui(cursor=shape)`` (a ``core/input/mouse_cursor.py`` shape) declares the
pointer's shape over a view: the innermost declaring view around the hovered
one wins, and the shape a press began with is kept until every button is up
(a text selection keeps the text cursor wherever the drag goes). No body runs
for it.
"""
from collections import namedtuple
import time

from meltygui.core.input.event import FOCUS, HOVER, KEYS, LEFT, MOVE, OUTSIDE, QUIET, SCROLL, TOUCH, WHEEL

# One touch contact: kind is 'down', 'move' or 'up'; contact tells fingers apart.
Touch = namedtuple('Touch', 'kind contact x y')
# Levers: how close in seconds and pixels presses must be to count as a multi click. A release
# within CLICK_PIXELS of its press is a click (`Event.left_mouse_clicked`).
CLICK_SECONDS = 0.4
CLICK_PIXELS = 4.0
BUTTONS = 3                      # left, right, middle: the buttons a view is told about


# Neither mouse button is claimed by an idle view.
_NO_CLAIM = (False, False)
_NO_KEYS = frozenset()


class ImGuiInput:
    def __init__(self, cache):
        self.cache = cache
        self.hover = self.capture = self.focus = self.mouse_owner = None
        self.characters = []
        self.text = ()
        self.sample = None
        self.serial = 0
        self.active = set()
        self.blur = None
        self.pointer_claims = {}
        self.settle_hover = set()
        self.right_active = set()
        self.pressed_keys = frozenset()
        # Event delivery (`deliver`): the key presses the host queued, the view holding the
        # pointer since a press (`grab`) and the one it hovers (`over`).
        self.keys = []
        self.grab = self.over = None
        # A view that asked about the left button is under the pointer or holds it: left drags are that view's, not a window move.
        self.left = False
        self.shape = None          # the declared pointer shape under the pointer, as of the last press while a button is held
        # Each button's last press, (time, x, y, clicks): what makes the next one a double or triple click.
        self.presses = [(0.0, 0.0, 0.0, 0)] * BUTTONS
        self.touches = []

    def character(self, codepoint):
        self.characters.append(codepoint)
        if self.cache.request_frame:
            self.cache.request_frame()

    def key(self, key, mods):
        """Host sink for one key press or repeat, in arrival order."""
        self.keys.append((key, mods))
        if self.cache.request_frame:
            self.cache.request_frame()

    def touch(self, kind, contact, x, y):
        """Host sink for one touch contact, in surface coordinates."""
        self.touches.append(Touch(kind, contact, x, y))
        if self.cache.request_frame:
            self.cache.request_frame()

    def local(self, node, position):
        """A surface position in `node`'s own coordinates."""
        cache = self.cache
        x, y = cache.position(node)
        return position[0] - cache.origin[0] - x, position[1] - cache.origin[1] - y

    def owner(self, node, asks):
        """The innermost view from `node` outward whose body asked about one of `asks` (bits of core/input/event.py)."""
        inputs, parent = self.cache.inputs, self.cache.graph.parent
        while node:
            event = inputs.get(node)
            if event is not None and event.asked & asks:
                return node
            node = parent(node)
        return None

    def taker(self, node, position, button):
        """The view that takes `button` at `position` over `node`: the innermost one around it that
        asked about that button, inside its own rectangle (a Melty window's title strip above it is the
        window's) and inside what it said it uses of that (`Event.reach`). Outside, the press is the
        window's: a left drag moves it."""
        node = self.owner(node, 1 << button)
        if node is not None:
            x, y = self.local(node, position)
            area = self.cache.inputs[node].area
            if area is None:
                area = (0.0, 0.0, *self.cache.graph.rect(node)[2:])
            left, top, right, bottom = area
            if not (left <= x < right and top <= y < bottom):
                return None
        return node

    def wake(self, node):
        """Run `node` again: its Event was told something. The run reads it and the executor clears it."""
        cache = self.cache
        cache.fresh.add(node)
        cache.geometry.frozen.discard(node)
        cache.graph.invalidate_node(node)
        if cache.request_frame:
            cache.request_frame()

    def count(self, button, position):
        """Which press in a row this one of `button` is: 1, 2 or 3 (the levers CLICK_SECONDS and CLICK_PIXELS)."""
        now = time.monotonic()
        then, x, y, clicks = self.presses[button]
        near = abs(position[0] - x) + abs(position[1] - y) <= CLICK_PIXELS
        if near and now - then <= CLICK_SECONDS:
            clicks = clicks % 3 + 1
        else:
            clicks = 1
        self.presses[button] = (now, position[0], position[1], clicks)
        return clicks

    def keyboard(self, node, has):
        """`node` gained (`has`) or lost the keyboard (core/input/focus.py): its Event says so."""
        event = self.cache.inputs.get(node)
        if event is None or event is QUIET or node not in self.cache.records:
            return
        event.keyboard = has
        if event.asked & FOCUS:
            # (Told to a view while it runs, the edge is cleared with that run: `is_focused` is what stays.)
            if has:
                event.gained = True
            else:
                event.lost = True
            self.wake(node)

    def deliver(self, position, buttons, old, wheel, modifiers, scrolled):
        """Turn this frame's sample into what the views' events say (core/input/event.py), and
        wake each view that was told something it asked about."""
        cache, inputs = self.cache, self.cache.inputs
        before = old[1] if old is not None else ()
        # The buttons that went down and up since the last sample (left, right, middle).
        pressed = [at for at in range(BUTTONS) if buttons[at] and not (at < len(before) and before[at])]
        released = [at for at in range(BUTTONS) if not buttons[at] and at < len(before) and before[at]]
        held = any(buttons[:BUTTONS])
        moved = old is None or old[0] != position
        if pressed or scrolled or any(wheel):
            target = self.hit(position)
            if pressed and self.grab is None:
                self.grab = self.taker(target, position, pressed[0])
            if 0 in pressed:
                cache.focus.take(self.owner(target, KEYS))
            if scrolled:
                node = self.owner(target, SCROLL)
                if node is not None:
                    inputs[node].scrolls = True
                    self.wake(node)
            elif any(wheel):
                node = self.owner(target, WHEEL)
                if node is not None:
                    event = inputs[node]
                    event.wheel_y += wheel[0]
                    event.wheel_x += wheel[1]
                    self.wake(node)
        counts = [self.count(button, position) for button in pressed]
        grab = self.grab
        event = inputs.get(grab)
        if event is not None and grab in cache.records:
            x, y = self.local(grab, position)
            told = False
            for button, clicks in zip(pressed, counts):
                bit = 1 << button
                if event.asked & bit:
                    event.downs |= bit
                    event.held |= bit
                    event.clicks = clicks
                    event.start_x = x
                    event.start_y = y
                    event.travel = 0.0
                    event.total_dx = 0.0
                    event.total_dy = 0.0
                    told = True
            if event.held and (pressed or released or moved):
                # Where the pointer is now, for what is told below. `dx` and `dy` add up until the view runs.
                if not pressed:
                    event.dx += x - event.x
                    event.dy += y - event.y
                event.total_dx = x - event.start_x
                event.total_dy = y - event.start_y
                event.travel = max(event.travel, abs(event.total_dx) + abs(event.total_dy))
                event.x = x
                event.y = y
                event.ctrl, event.shift, event.alt = modifiers[:3]
            for button in released:
                bit = 1 << button
                if event.held & bit:
                    event.held &= ~bit
                    event.ups |= bit
                    if event.travel <= CLICK_PIXELS:
                        event.taps |= bit
                    told = True
            if held and not pressed and moved and event.held:
                event.drags |= event.held
                told = True
            elif held and not pressed and event.held and event.asked & OUTSIDE:
                # Held still: only a pointer resting outside the view needs time to pass.
                _, _, wide, tall = cache.graph.rect(grab)
                if not (0 <= x < wide and 0 <= y < tall):
                    event.rests |= event.held
                    told = True
            if told:
                self.wake(grab)
        if not held:
            self.grab = None
        # Hover goes to the innermost view around the pointer that asked about it. It follows the
        # pointer's moves and a press, and stays where it is while a button is held.
        frozen = held and not pressed
        if (moved or pressed) and not frozen or self.over is not None:
            over = self.over if frozen else self.owner(self.hit(position), HOVER | MOVE)
            if over != self.over:
                event = inputs.get(self.over)
                if event is not None and self.over in cache.records:
                    event.over = False
                    self.wake(self.over)
                self.over = over
            if over is not None and not frozen:
                event = inputs[over]
                if not event.over:
                    event.over = True
                    event.moves = moved
                    event.x, event.y = self.local(over, position)
                    event.ctrl, event.shift, event.alt = modifiers[:3]
                    self.wake(over)
                elif moved and not held and event.asked & MOVE:
                    event.moves = True
                    event.x, event.y = self.local(over, position)
                    event.ctrl, event.shift, event.alt = modifiers[:3]
                    self.wake(over)
        for touch in self.touches:
            target = self.owner(self.hit((touch.x, touch.y)), TOUCH)
            if target is not None:
                x, y = self.local(target, (touch.x, touch.y))
                event = inputs[target]
                if not event.contacts:
                    event.contacts = []
                event.contacts.append(touch._replace(x=x, y=y))
                self.wake(target)
        self.touches.clear()
        if self.keys or self.text:
            keyboard = cache.focus.keyboard
            event = inputs.get(keyboard)
            if event is not None and keyboard in cache.records:
                if self.keys:
                    event.pressed = [*event.pressed, *self.keys]
                if self.text:
                    event.typed += ''.join(map(chr, self.text))
                self.wake(keyboard)

    def hit(self, position):
        cache = self.cache
        x, y = position[0] - cache.origin[0], position[1] - cache.origin[1]
        _, hits = cache.graph.hits(x, y)
        return hits[0][0] if hits else None

    def process(self, io):
        self.serial += 1
        position = tuple(io.mouse_pos)
        buttons = tuple(io.mouse_down)
        keys = tuple(io.keys_down)
        # Idle frames have no key down; skip enumerating the whole key table.
        self.pressed_keys = frozenset(i for i, down in enumerate(keys) if down) if any(keys) else _NO_KEYS
        modifiers = tuple(getattr(io, 'key_' + name) for name in ('ctrl', 'shift', 'alt', 'super'))
        wheel = (io.mouse_wheel, io.mouse_wheel_horizontal)
        scrolled = any(wheel) and not any(buttons) and self.cache.scroll_at(position, (-wheel[1]*40., -wheel[0]*40.))
        if scrolled:
            wheel = (0., 0.)
        old = self.sample
        self.sample = (position, buttons, keys, modifiers, wheel)
        self.text, self.characters = tuple(self.characters), []
        if old != self.sample or self.text or any(buttons) or self.pressed_keys:
            self.cache.calibration.input()
        inputs, focus = self.cache.inputs, self.cache.focus
        pressed = any(down and (old is None or not old[1][i]) for i, down in enumerate(buttons))
        if focus.stack or focus.shut:
            # Open popups: a press outside one closes it, Escape the innermost (core/input/focus.py).
            if pressed:
                focus.press(self.hit(position))
            if self.keys:
                focus.escape(self.keys)
        if inputs:
            self.deliver(position, buttons, old, wheel, modifiers, scrolled)
        if self.keys:
            self.keys.clear()
        # During a wheel gesture keep cached content frozen. Settle hover once
        # the wheel stops; keyboard edits and captured widgets still get input.
        if scrolled and old is not None and old[2:4] == self.sample[2:4] and not self.text and not self.active:
            return
        previous_hover, previous_focus, previous_blur = self.hover, self.focus, self.blur
        self.blur = None
        self.hover = None if self.cache.drag else self.hit(position)
        if pressed:
            self.capture = self.hover
            self.focus = self.hover
        self.mouse_owner = self.capture if self.capture is not None else self.hover
        # Left drags are a view's, not a window move, when the view that holds the pointer, or else
        # the one under it, asked about the left button.
        if not inputs:
            self.left = False
        elif self.grab is not None:
            self.left = inputs[self.grab].asked & LEFT != 0
        else:
            self.left = self.taker(self.hover, position, 0) is not None
        cursors = self.cache.cursors
        if cursors:
            # The declared pointer shape: the innermost around the hovered view, held from a press to the release.
            if pressed or not any(buttons):
                node, parent = self.hover, self.cache.graph.parent
                while node and node not in cursors:
                    node = parent(node)
                self.shape = cursors[node] if node else None
            if self.shape is not None:
                from meltygui.core.input import mouse_cursor
                mouse_cursor.request(self.shape)
        dirty = {previous_blur}  # settle the old context's synthetic outside click
        if old is None or old[:2] != self.sample[:2] or any(wheel) or self.hover != previous_hover:
            dirty.update((previous_hover, self.hover, self.capture))
        if previous_focus != self.focus:
            dirty.update((previous_focus, self.focus))
            self.blur = previous_focus
        if old is None or old[2:4] != self.sample[2:4] or self.text:
            dirty.add(self.focus)
        dirty.update(self.active)
        dirty.update(self.settle_hover)
        self.settle_hover.clear()
        for node in dirty:
            # A view with an Event runs for what it was told, never on raw samples.
            if node in self.cache.records and node not in inputs:
                self.cache.geometry.frozen.discard(node)
                self.cache.graph.invalidate_node(node)
        if not any(buttons):
            self.capture = None

    def finish(self, node, capture):
        active = capture.active_widget
        if not active and node != self.mouse_owner and node != self.focus:
            # A view that owns no live widget, pointer or focus claims nothing
            # and cannot tick; skip reading the frame's input state for it.
            if self.right_active: self.right_active.discard(node)
            self.pointer_claims[node] = _NO_CLAIM
            if self.active: self.active.discard(node)
            return
        import meltygui_imgui as imgui
        io = imgui.get_io()
        hovered = node == self.mouse_owner and imgui.is_any_item_hovered()
        if active:
            self.focus = node
            if any(io.mouse_down):
                self.capture = self.mouse_owner = node
        if active and imgui.is_item_activated():
            if io.mouse_down[1]:
                self.right_active.add(node)
            else:
                self.right_active.discard(node)
        if not active:
            self.right_active.discard(node)
        # A hit cache rectangle is an input destination, not an interactive
        # item. Text and empty layout space must leave host gestures available.
        # Ordinary widgets own left gestures. Right gestures belong to the
        # host unless ImGui actually activated an item with the right button.
        self.pointer_claims[node] = (hovered or (active and io.mouse_down[0]),
                                     node in self.right_active and io.mouse_down[1])
        # IsAnyItemHovered includes the previous ImGui frame's hovered ID.
        # Settle it once after pointer motion, otherwise a clean cached view
        # could retain that obsolete claim indefinitely over empty space.
        if hovered and (io.mouse_delta.x or io.mouse_delta.y):
            self.settle_hover.add(node)
            if self.cache.request_frame:
                self.cache.request_frame()
        # A held drag/key or an active text field can change output with time.
        # Hover alone does not need a continuous redraw.
        ticking = node == self.focus and (io.want_text_input or
                  (active and
                   (any(io.mouse_down) or any(io.keys_down))))
        if ticking:
            self.active.add(node)
            if self.cache.request_frame:
                self.cache.request_frame()
        else:
            self.active.discard(node)

    def retire(self, node):
        self.active.discard(node)
        self.settle_hover.discard(node)
        self.right_active.discard(node)
        self.pointer_claims.pop(node, None)
        for name in ('hover', 'capture', 'focus', 'mouse_owner', 'blur', 'grab', 'over'):
            if getattr(self, name) == node:
                setattr(self, name, None)
        if self.cache is not None:
            self.cache.focus.retire(node)
