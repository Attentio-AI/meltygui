"""Retained-view invalidation using the one host ImGui context's input.

ImGui owns widget IDs, keyboard focus and editing state globally. This bridge
tracks which retained views must execute; it never copies IO into view contexts.
"""


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

    def character(self, codepoint):
        self.characters.append(codepoint)
        if self.cache.request_frame:
            self.cache.request_frame()

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
        # During a wheel gesture keep cached content frozen. Settle hover once
        # the wheel stops; keyboard edits and captured widgets still get input.
        if scrolled and old is not None and old[2:4] == self.sample[2:4] and not self.text and not self.active:
            return
        previous_hover, previous_focus, previous_blur = self.hover, self.focus, self.blur
        self.blur = None
        self.hover = None if self.cache.drag else self.hit(position)
        pressed = any(down and (old is None or not old[1][i]) for i, down in enumerate(buttons))
        if pressed:
            self.capture = self.hover
            self.focus = self.hover
        self.mouse_owner = self.capture if self.capture is not None else self.hover
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
            if node in self.cache.records:
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
        for name in ('hover', 'capture', 'focus', 'mouse_owner', 'blur'):
            if getattr(self, name) == node:
                setattr(self, name, None)
