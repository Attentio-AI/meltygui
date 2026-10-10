"""Host input routed to independent retained ImGui contexts (prototype).

The invalidation unit is the view rectangle, not a catalog of widget functions.
ImGui owns widget IDs, focus and editing state inside each context. This bridge
owns pointer capture and keyboard focus *between* cached views.
"""


def _empty_clipboard():
    return ''


def _ignore_clipboard(text):
    pass


class ImGuiInput:
    def __init__(self, cache):
        self.cache = cache
        self.hover = self.capture = self.focus = self.mouse_owner = None
        self.characters = []
        self.text = ()
        self.sample = None
        self.host_io = None
        self.serial = 0
        self.consumed = {}
        self.active = set()
        self.clipboard = None
        self.blur = None
        self.pointer_claims = {}
        self.settle_hover = set()
        self.right_active = set()
        self.key_map = ()
        self.pressed_keys = frozenset()
        self.applied_key_maps = {}
        self.delivered_keys = {}

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
        self.host_io = io
        self.serial += 1
        position = tuple(io.mouse_pos)
        buttons = tuple(io.mouse_down)
        keys = tuple(io.keys_down)
        self.key_map = tuple(io.key_map)
        self.pressed_keys = frozenset(i for i, down in enumerate(keys) if down)
        modifiers = tuple(getattr(io, 'key_' + name) for name in ('ctrl', 'shift', 'alt', 'super'))
        wheel = (io.mouse_wheel, io.mouse_wheel_horizontal)
        old = self.sample
        self.sample = (position, buttons, keys, modifiers, wheel)
        self.text, self.characters = tuple(self.characters), []
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

    def apply(self, node, io):
        host = self.host_io
        if host is None:
            return
        io.delta_time = max(1 / 1000, min(host.delta_time, .1))
        io.font_global_scale = host.font_global_scale
        if self.applied_key_maps.get(node) != self.key_map:
            key_map = io.key_map
            for i, key in enumerate(self.key_map):
                key_map[i] = key
            self.applied_key_maps[node] = self.key_map
        if self.clipboard is not None:
            io.get_clipboard_text_fn, io.set_clipboard_text_fn = self.clipboard
        position, buttons, keys, modifiers, wheel = self.sample
        if node == self.mouse_owner:
            x, y = self.cache._position(node)
            io.mouse_pos = (position[0] - self.cache.origin[0] - x,
                            position[1] - self.cache.origin[1] - y)
        else:
            io.mouse_pos = (-1e20, -1e20)
        for i, down in enumerate(buttons):
            # A click in another cached context must deactivate the old editor.
            # Its off-view pointer ensures it cannot activate a covered widget.
            io.mouse_down[i] = down and node in (self.mouse_owner, self.blur)
        pressed = self.pressed_keys if node == self.focus else frozenset()
        previous = self.delivered_keys.get(node, frozenset())
        if pressed != previous:
            # Each property access creates a binding array view. More importantly,
            # unfocused contexts need no 512-slot keyboard copy on every capture.
            keys_down = io.keys_down
            for key in pressed ^ previous:
                keys_down[key] = key in pressed
            self.delivered_keys[node] = pressed
        for name, value in zip(('ctrl', 'shift', 'alt', 'super'), modifiers):
            setattr(io, 'key_' + name, value and node == self.focus)
        fresh = self.consumed.get(node) != self.serial
        io.mouse_wheel = wheel[0] if fresh and node == self.mouse_owner else 0
        io.mouse_wheel_horizontal = wheel[1] if fresh and node == self.mouse_owner else 0
        io.clear_input_characters()
        if fresh and node == self.focus:
            for codepoint in self.text:
                io.add_input_character(codepoint)
        self.consumed[node] = self.serial

    def finish(self, node):
        import meltygui_imgui as imgui
        io = imgui.get_io()
        hovered = imgui.is_any_item_hovered()
        active = imgui.is_any_item_active()
        if imgui.is_item_activated():
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
        self.consumed.pop(node, None)
        self.applied_key_maps.pop(node, None)
        self.delivered_keys.pop(node, None)
        for name in ('hover', 'capture', 'focus', 'mouse_owner', 'blur'):
            if getattr(self, name) == node:
                setattr(self, name, None)

    @staticmethod
    def release_context_callbacks():
        import meltygui_imgui as imgui
        # This binding's destroy_context does not remove its Python clipboard
        # callback table. Drop references to the owning backend before teardown.
        io = imgui.get_io()
        io.get_clipboard_text_fn = _empty_clipboard
        io.set_clipboard_text_fn = _ignore_clipboard
