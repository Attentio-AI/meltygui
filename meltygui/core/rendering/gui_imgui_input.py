"""Retained-view invalidation using the one host ImGui context's input.

ImGui owns widget IDs, keyboard focus and editing state globally. This bridge
tracks which retained views must execute; it never copies IO into view contexts.

Views declared with ``@gui(listen=(...))`` are event driven instead: raw pointer
and key samples never rerun them, and each name they list arrives as a list in
``view_events``:

- ``keys``: ``(key, mods)`` presses and repeats, for the keyboard owner.
- ``text``: typed strings, for the keyboard owner.
- ``focus``: True / False when the view gains / loses the keyboard.
- ``pointer``: ``Pointer`` press, drag and release of any button, in the
  view's own coordinates.
- ``hold``: ``Pointer`` of kind 'hold', once a frame while a button is held
  and the pointer rests outside the view (what a drag past an edge needs to
  keep scrolling).
- ``hover``: ``Pointer`` of kind 'hover' when the pointer moves over the view
  with no button held, and one of kind 'leave' when it has left.
- ``touch``: ``Touch`` contacts, from hosts that report them through
  ``ImGuiInput.touch``.
- ``wheel``: ``(horizontal, vertical)`` steps no viewport consumed.
- ``scroll``: True after a viewport under the pointer scrolled.

A left press gives the keyboard, any press the pointer, to the innermost
listening view around the hit one, so popovers and inline widgets leave the
keyboard with their editor. Views that list nothing cost one dictionary truth
test a frame. Who has the keyboard, and the temporary popups a press or Escape
closes, are `cache.focus` (core/input/focus.py).

Window gestures are the host's by default, over every view: a left drag moves
the Melty or OS window, a right drag resizes it. A view overrides the left
one by listening for ``pointer`` (``left``: text selection, a list's rows),
over its whole rectangle or over the part of it the view says it uses
(``reach``: an editor's rows, so the empty space below them still moves the
window); nothing a view listens for overrides the right one. An ImGui item still owns
the button that activated it (``finish``).

``@gui(cursor=shape)`` (a ``core/input/mouse_cursor.py`` shape) declares the
pointer's shape over a view: the innermost declaring view around the hovered
one wins, and the shape a press began with is kept until every button is up
(a text selection keeps the text cursor wherever the drag goes). No body runs
for it.
"""
from collections import namedtuple
import time

# kind is 'press', 'drag', 'release', 'hold', 'hover' or 'leave'; clicks counts
# 1, 2, 3 for repeated left presses; button is 0 left, 1 right, 2 middle (the
# one pressed or released, else the first one held).
Pointer = namedtuple('Pointer', 'kind x y clicks ctrl shift alt button', defaults=(0,))
# One touch contact: kind is 'down', 'move' or 'up'; contact tells fingers apart.
Touch = namedtuple('Touch', 'kind contact x y')
# Levers: how close in seconds and pixels presses must be to count as a multi click.
CLICK_SECONDS = 0.4
CLICK_PIXELS = 4.


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
        # Event delivery to listening views: presses queued by the host, the
        # views owning the keyboard and the held pointer, the last press.
        self.keys = []
        self.grab = self.over = None
        # A view that listens for the pointer is under it or holds it: left drags are that view's, not a window move.
        self.left = False
        self.shape = None          # the declared pointer shape under the pointer, as of the last press while a button is held
        # View to the part of its rectangle it takes the pointer in, (x0, y0, x1, y1) in its own coordinates (`reach`).
        self.reaches = {}
        self.press = (0., 0., 0., 0)
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

    def owner(self, node, name):
        """The innermost view from `node` outward that listens for `name`."""
        listeners, parent = self.cache.listeners, self.cache.graph.parent
        while node:
            if name in listeners.get(node, ()):
                return node
            node = parent(node)
        return None

    def reach(self, node, rect=None):
        """A listening view says where it takes the pointer: `rect` (x0, y0, x1, y1) in its own coordinates,
        None for its whole rectangle. Outside it presses and left drags are the window's (it moves)."""
        if rect is None:
            self.reaches.pop(node, None)
        else:
            self.reaches[node] = rect

    def taker(self, node, position):
        """The view that takes the pointer at `position` over `node`: the innermost one around it that
        listens for it, inside its own rectangle (a Melty window's title strip above it is the window's)
        and inside what it said it uses of that (`reach`)."""
        node = self.owner(node, 'pointer')
        if node is not None:
            x, y = self.local(node, position)
            left, top, right, bottom = self.reaches.get(node) or (0., 0., *self.cache.graph.rect(node)[2:])
            if not (left <= x < right and top <= y < bottom):
                return None
        return node

    def post(self, node, name, item):
        """Queue one event; the view's next run reads the list in view_events[name]."""
        cache = self.cache
        if node is None or node not in cache.records:
            return
        cache.events.setdefault(node, {}).setdefault(name, []).append(item)
        cache.geometry.frozen.discard(node)
        cache.graph.invalidate_node(node)
        if cache.request_frame:
            cache.request_frame()

    def deliver(self, position, buttons, old, wheel, modifiers, scrolled):
        """Turn this frame's sample into events for the listening views."""
        cache, listeners = self.cache, self.cache.listeners
        before = old[1] if old is not None else ()
        # The buttons that went down and up since the last sample (left, right, middle).
        pressed = [at for at in range(3) if buttons[at] and not (at < len(before) and before[at])]
        released = [at for at in range(3) if not buttons[at] and at < len(before) and before[at]]
        held = next((at for at in range(3) if buttons[at]), None)
        moved = old is None or old[0] != position
        if pressed or scrolled or any(wheel):
            target = self.hit(position)
            if pressed and self.grab is None:
                self.grab = self.taker(target, position)
            if 0 in pressed:
                cache.focus.take(self.owner(target, 'keys'))
                now = time.monotonic()
                then, x, y, clicks = self.press
                near = abs(position[0] - x) + abs(position[1] - y) <= CLICK_PIXELS
                self.press = (now, position[0], position[1],
                              clicks % 3 + 1 if near and now - then <= CLICK_SECONDS else 1)
            if scrolled:
                self.post(self.owner(target, 'scroll'), 'scroll', True)
            elif any(wheel):
                self.post(self.owner(target, 'wheel'), 'wheel', (wheel[1], wheel[0]))
        grab = self.grab
        if grab is not None and grab in cache.records:
            x, y = self.local(grab, position)
            for kind, changed in (('press', pressed), ('release', released)):
                for button in changed:
                    self.post(grab, 'pointer', Pointer(kind, x, y, self.press[3], *modifiers[:3], button))
            if held is not None and not pressed and moved:
                self.post(grab, 'pointer', Pointer('drag', x, y, self.press[3], *modifiers[:3], held))
            elif held is not None and not pressed and 'hold' in listeners.get(grab, ()):
                # Held still: only a pointer resting outside the view needs time to pass.
                _, _, wide, tall = cache.graph.rect(grab)
                if not (0 <= x < wide and 0 <= y < tall):
                    self.post(grab, 'hold', Pointer('hold', x, y, self.press[3], *modifiers[:3], held))
        if held is None:
            self.grab = None
        if moved and held is None or self.over is not None:
            # Hover goes to the innermost view around the pointer that asked for it.
            over = self.owner(self.hit(position), 'hover') if held is None else self.over
            if over != self.over:
                if self.over in cache.records:
                    self.post(self.over, 'hover', Pointer('leave', 0., 0., 0, *modifiers[:3]))
                self.over = over
            if over is not None and moved and held is None:
                self.post(over, 'hover', Pointer('hover', *self.local(over, position), 0, *modifiers[:3]))
        for touch in self.touches:
            target = self.owner(self.hit((touch.x, touch.y)), 'touch')
            if target is not None:
                self.post(target, 'touch', touch._replace(**dict(zip('xy', self.local(target, (touch.x, touch.y))))))
        self.touches.clear()
        if self.keys or self.text:
            keyboard = cache.focus.keyboard
            for item in self.keys:
                self.post(keyboard, 'keys', item)
            if self.text:
                self.post(keyboard, 'text', ''.join(map(chr, self.text)))

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
        listeners, focus = self.cache.listeners, self.cache.focus
        pressed = any(down and (old is None or not old[1][i]) for i, down in enumerate(buttons))
        if focus.stack or focus.shut:
            # Open popups: a press outside one closes it, Escape the innermost (core/input/focus.py).
            if pressed:
                focus.press(self.hit(position))
            if self.keys:
                focus.escape(self.keys)
        if listeners:
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
        self.left = bool(listeners) and (self.grab is not None or self.taker(self.hover, position) is not None)
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
            # Listening views run on their events, never on raw samples.
            if node in self.cache.records and node not in listeners:
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
        self.reaches.pop(node, None)
        for name in ('hover', 'capture', 'focus', 'mouse_owner', 'blur', 'grab', 'over'):
            if getattr(self, name) == node:
                setattr(self, name, None)
        if self.cache is not None:
            self.cache.focus.retire(node)
