"""
Cross-platform userspace input backend using pynput.

Works on Linux (X11/Wayland), macOS, and Windows without root/admin.

Requirements:
    pip install pynput

Note: pynput uses OS-level hooks:
    - Linux: Xlib (X11) or uinput
    - macOS: Quartz event taps  
    - Windows: Win32 hooks
"""

from __future__ import annotations
import threading
from collections import deque
from typing import TYPE_CHECKING
import time

import glfw

try:
    from pynput import mouse, keyboard
    from pynput.keyboard import Key, KeyCode
    from pynput.mouse import Button
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False
    mouse = keyboard = Key = KeyCode = Button = None

if TYPE_CHECKING:
    from input_handler import InputHandler


# =============================================================================
# Key Mappings
# =============================================================================

def _key_to_id(key) -> str:
    """Convert pynput key to input_id string."""
    if not HAS_PYNPUT:
        return str(key)
    
    # Regular character keys
    if isinstance(key, KeyCode):
        if key.char:
            return key.char.lower()
        # Virtual key code fallback
        if key.vk:
            return f"vk_{key.vk}"
        return "unknown"
    
    # Special keys
    if isinstance(key, Key):
        mapping = {
            Key.space: "space",
            Key.enter: "enter",
            Key.tab: "tab",
            Key.backspace: "backspace",
            Key.esc: "escape",
            Key.delete: "delete",
            Key.insert: "insert",
            Key.home: "home",
            Key.end: "end",
            Key.page_up: "page_up",
            Key.page_down: "page_down",
            Key.up: "up",
            Key.down: "down",
            Key.left: "left",
            Key.right: "right",
            Key.shift: "left_shift",
            Key.shift_r: "right_shift",
            Key.ctrl: "left_ctrl",
            Key.ctrl_r: "right_ctrl",
            Key.alt: "left_alt",
            Key.alt_r: "right_alt",
            Key.cmd: "left_meta",
            Key.cmd_r: "right_meta",
            Key.caps_lock: "caps_lock",
            Key.num_lock: "num_lock",
            Key.scroll_lock: "scroll_lock",
            Key.print_screen: "print_screen",
            Key.pause: "pause",
            Key.menu: "menu",
        }
        
        if key in mapping:
            return mapping[key]
        
        # Function keys
        name = key.name if hasattr(key, 'name') else str(key)
        if name.startswith("f") and name[1:].isdigit():
            return name
        
        return name.lower().replace("key.", "")
    
    return str(key).lower()


"""
Dear ImGui input backend.

Works with pyimgui or the imgui bundle (imgui[glfw], imgui[sdl2], etc.)

Requirements:
    pip install imgui[glfw]  # or imgui[sdl2], pyimgui, etc.

Note: ImGui receives input from a windowing backend (GLFW, SDL2, etc.)
      This backend reads ImGui's IO state and converts it to InputHandler events.
"""

from typing import TYPE_CHECKING
import time

try:
    import imgui

    HAS_IMGUI = True
except ImportError:
    HAS_IMGUI = False
    imgui = None

if TYPE_CHECKING:
    from input_handler import InputHandler




def _button_to_id(button) -> str:
    """Convert pynput mouse button to input_id string."""
    if not HAS_PYNPUT:
        return str(button)
    
    mapping = {
        Button.left: "left_mouse",
        Button.right: "right_mouse",
        Button.middle: "middle_mouse",
    }
    
    if button in mapping:
        return mapping[button]
    
    # Extra buttons (Button.x1, Button.x2, etc.)
    name = button.name if hasattr(button, 'name') else str(button)
    return f"mouse_{name}"


# =============================================================================
# Raw Event Container
# =============================================================================

class RawEvent:
    __slots__ = ('type', 'input_id', 'value', 'x', 'y', 'dx', 'dy', 't')
    
    DOWN = 0
    UP = 1
    MOVE = 2
    SCROLL = 3
    
    def __init__(self, type_: int, input_id: str, value: float,
                 x: float, y: float, dx: float, dy: float, t: float):
        self.type = type_
        self.input_id = input_id
        self.value = value
        self.x = x
        self.y = y
        self.dx = dx
        self.dy = dy
        self.t = t


# =============================================================================
# ImGui Backend (State-Based)
# =============================================================================

class ImGuiBackend:
    """
    Backend that reads Dear ImGui's IO state.

    ImGui maintains input state rather than emitting events, so this backend
    tracks state changes between frames and generates corresponding events.

    Usage:
        handler = InputHandler()
        backend = ImGuiBackend(handler)

        while running:
            imgui.new_frame()  # ImGui updates IO state here

            handler.begin_frame()
            handler.register_hovered(...)

            backend.pump()  # Read IO state, generate events

            events = handler.process_frame()

            # ... render imgui ...
            imgui.render()
    """

    # ImGui mouse button indices
    MOUSE_BUTTONS = {
        0: "left_mouse",
        1: "right_mouse",
        2: "middle_mouse",
        3: "mouse_4",
        4: "mouse_5",
    }

    # ImGui key indices (imgui.KEY_*)
    # This mapping covers common keys; extend as needed
    KEY_NAMES = {
        glfw.KEY_TAB: "tab",
        glfw.KEY_LEFT: "left",
        glfw.KEY_RIGHT: "right",
        glfw.KEY_UP: "up",
        glfw.KEY_DOWN: "down",
        glfw.KEY_PAGE_UP: "page_up",
        glfw.KEY_PAGE_DOWN: "page_down",
        glfw.KEY_HOME: "home",
        glfw.KEY_END: "end",
        glfw.KEY_INSERT: "insert",
        glfw.KEY_DELETE: "delete",
        glfw.KEY_BACKSPACE: "backspace",
        glfw.KEY_SPACE: "space",
        glfw.KEY_ENTER: "enter",
        glfw.KEY_ESCAPE: "escape",
        glfw.KEY_A: "a",
        glfw.KEY_B: "b",
        glfw.KEY_C: "c",
        glfw.KEY_V: "v",
        glfw.KEY_F: "f",
        glfw.KEY_X: "x",
        glfw.KEY_Y: "y",
        glfw.KEY_S: "s",
        glfw.KEY_Z: "z",
    } if HAS_IMGUI else {}

    def __init__(self, handler: InputHandler):
        if not HAS_IMGUI:
            raise ImportError("pip install imgui[glfw]  # or imgui[sdl2], pyimgui")

        self.handler = handler

        # Previous frame state for change detection
        self._prev_mouse_pos = (0.0, 0.0)
        self._prev_mouse_down = [False] * 5
        self._prev_keys_down: set[int] = set()
        self._prev_scroll = (0.0, 0.0)

    def pump(self):

        """
        Read ImGui IO state and generate events for state changes.
        Call once per frame after imgui.new_frame().
        """
        io = imgui.get_io()
        t = time.perf_counter()

        # --- Mouse Position ---
        mx, my = io.mouse_pos
        px, py = self._prev_mouse_pos

        if (mx, my) != (px, py) and mx >= 0 and my >= 0:
            dx, dy = mx - px, my - py
            self.handler.feed_move(mx, my, dx, dy, t)

        self._prev_mouse_pos = (mx, my)

        # --- Mouse Buttons ---
        for i in range(5):
            input_id = self.MOUSE_BUTTONS.get(i, f"mouse_{i}")
            is_down = io.mouse_down[i]
            was_down = self._prev_mouse_down[i]

            if is_down and not was_down:
                self.handler.feed_down(input_id, mx, my, t)
            elif not is_down and was_down:
                self.handler.feed_up(input_id, mx, my, t)

            self._prev_mouse_down[i] = is_down

        # --- Mouse Wheel ---
        wheel_x = io.mouse_wheel_horizontal
        wheel_y = io.mouse_wheel

        if wheel_y != 0:
            self.handler.feed_change("scroll_y", wheel_y, t)
        if wheel_x != 0:
            self.handler.feed_change("scroll_x", wheel_x, t)

        # --- Keyboard ---
        current_keys: set[int] = set()

        for key_index, key_name in self.KEY_NAMES.items():
            if io.keys_down[key_index]:
                current_keys.add(key_index)

        # Keys pressed this frame
        for key_index in current_keys - self._prev_keys_down:
            key_name = self.KEY_NAMES.get(key_index, f"key_{key_index}")
            self.handler.feed_down(key_name, mx, my, t)

        # Keys released this frame
        for key_index in self._prev_keys_down - current_keys:
            key_name = self.KEY_NAMES.get(key_index, f"key_{key_index}")
            self.handler.feed_up(key_name, mx, my, t)

        self._prev_keys_down = current_keys

        # --- Modifiers ---
        self.handler.set_modifiers(
            shift=io.key_shift,
            ctrl=io.key_ctrl,
            alt=io.key_alt,
            meta=io.key_super,
        )

    @property
    def allow_hovering(self) -> bool:
        """True if ImGui allows hovering (not blocking input)."""
        return not imgui.is_window_hovered()


# =============================================================================
# Pynput Backend
# =============================================================================

class PynputBackend:
    """
    Cross-platform userspace input backend.
    
    Usage:
        handler = InputHandler()
        backend = PynputBackend(handler)
        backend.start()
        
        while running:
            handler.begin_frame()
            handler.register_hovered(...)
            backend.pump()
            events = handler.process_frame()
        
        backend.stop()
    """
    
    def __init__(self, handler: InputHandler):
        if not HAS_PYNPUT:
            raise ImportError("pip install pynput")
        
        self.handler = handler
        self._queue: deque[RawEvent] = deque(maxlen=1024)
        self._lock = threading.Lock()
        
        self._cursor_x = 0.0
        self._cursor_y = 0.0
        
        self._shift = False
        self._ctrl = False
        self._alt = False
        self._meta = False
        
        self._mouse_listener: mouse.Listener | None = None
        self._keyboard_listener: keyboard.Listener | None = None
    
    def start(self):
        """Start listening for input events."""
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        
        self._mouse_listener.start()
        self._keyboard_listener.start()
    
    def stop(self):
        """Stop listening."""
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
    
    def pump(self):
        """Transfer accumulated events to handler. Call once per frame."""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            self.handler.set_modifiers(self._shift, self._ctrl, self._alt, self._meta)
        
        # Process events in order to preserve drag semantics
        for e in events:
            if e.type == RawEvent.DOWN:
                self.handler.feed_down(e.input_id, e.x, e.y, e.t)
            elif e.type == RawEvent.UP:
                self.handler.feed_up(e.input_id, e.x, e.y, e.t)
            elif e.type == RawEvent.MOVE:
                self.handler.feed_move(e.x, e.y, e.dx, e.dy, e.t)
            elif e.type == RawEvent.SCROLL:
                self.handler.feed_change(e.input_id, e.value, e.t)
    
    # -------------------------------------------------------------------------
    # Mouse callbacks (run in pynput's thread)
    # -------------------------------------------------------------------------
    
    def _on_move(self, x: int, y: int):
        t = time.perf_counter()
        with self._lock:
            dx = x - self._cursor_x
            dy = y - self._cursor_y
            self._cursor_x = float(x)
            self._cursor_y = float(y)
            self._queue.append(RawEvent(
                RawEvent.MOVE, "cursor", 0.0, x, y, dx, dy, t
            ))
    
    def _on_click(self, x: int, y: int, button, pressed: bool):
        t = time.perf_counter()
        input_id = _button_to_id(button)
        with self._lock:
            self._cursor_x = float(x)
            self._cursor_y = float(y)
            
            if pressed:
                self._queue.append(RawEvent(
                    RawEvent.DOWN, input_id, 1.0, x, y, 0, 0, t
                ))
            else:
                self._queue.append(RawEvent(
                    RawEvent.UP, input_id, 0.0, x, y, 0, 0, t
                ))
    
    def _on_scroll(self, x: int, y: int, dx: int, dy: int):
        t = time.perf_counter()
        with self._lock:
            self._cursor_x = float(x)
            self._cursor_y = float(y)
            
            if dy != 0:
                self._queue.append(RawEvent(
                    RawEvent.SCROLL, "scroll_y", float(dy), x, y, 0, 0, t
                ))
            if dx != 0:
                self._queue.append(RawEvent(
                    RawEvent.SCROLL, "scroll_x", float(dx), x, y, 0, 0, t
                ))
    
    # -------------------------------------------------------------------------
    # Keyboard callbacks (run in pynput's thread)
    # -------------------------------------------------------------------------
    
    def _on_press(self, key):
        t = time.perf_counter()
        input_id = _key_to_id(key)
        self._update_modifiers(input_id, True)
        
        with self._lock:
            self._queue.append(RawEvent(
                RawEvent.DOWN, input_id, 1.0,
                self._cursor_x, self._cursor_y, 0, 0, t
            ))
    
    def _on_release(self, key):
        t = time.perf_counter()
        input_id = _key_to_id(key)
        self._update_modifiers(input_id, False)
        
        with self._lock:
            self._queue.append(RawEvent(
                RawEvent.UP, input_id, 0.0,
                self._cursor_x, self._cursor_y, 0, 0, t
            ))
    
    def _update_modifiers(self, input_id: str, pressed: bool):
        if "shift" in input_id:
            self._shift = pressed
        elif "ctrl" in input_id:
            self._ctrl = pressed
        elif "alt" in input_id:
            self._alt = pressed
        elif "meta" in input_id:
            self._meta = pressed
    
    def set_cursor(self, x: float, y: float):
        """Manually set cursor position if needed."""
        with self._lock:
            self._cursor_x = x
            self._cursor_y = y

    @property
    def allow_hovering(self) -> bool:
        """True if ImGui allows hovering (not blocking input)."""
        return True


# =============================================================================
# Window-Integrated Backend (for toolkit integration)
# =============================================================================

class JsonBackend:
    """
    Backend that receives events as JSON/dict.
    
    Use this to integrate with any windowing toolkit (pygame, glfw, sdl, tkinter, etc.)
    by converting their events to a simple dict format.
    
    Usage:
        handler = InputHandler()
        backend = JsonBackend(handler)
        
        # In your toolkit's event loop:
        for event in toolkit_events:
            backend.push({
                "type": "down",  # or "up", "move", "scroll"
                "input": "left_mouse",  # or key name
                "x": event.x,
                "y": event.y,
            })
        
        backend.pump()
        events = handler.process_frame()
    """
    
    def __init__(self, handler: InputHandler):
        self.handler = handler
        self._queue: deque[dict] = deque(maxlen=1024)
        self._cursor_x = 0.0
        self._cursor_y = 0.0
    
    def push(self, event: dict):
        """
        Push an event dict:
            {"type": "down"|"up"|"move"|"scroll", "input": str, "x": float, "y": float, "value": float}
        """
        self._queue.append(event)
    
    def push_down(self, input_id: str, x: float = None, y: float = None):
        self._queue.append({"type": "down", "input": input_id, 
                          "x": x if x is not None else self._cursor_x,
                          "y": y if y is not None else self._cursor_y})
    
    def push_up(self, input_id: str, x: float = None, y: float = None):
        self._queue.append({"type": "up", "input": input_id,
                          "x": x if x is not None else self._cursor_x,
                          "y": y if y is not None else self._cursor_y})
    
    def push_move(self, x: float, y: float, dx: float = None, dy: float = None):
        if dx is None:
            dx = x - self._cursor_x
        if dy is None:
            dy = y - self._cursor_y
        self._cursor_x = x
        self._cursor_y = y
        # Queue move events to preserve ordering with button events
        self._queue.append({"type": "move", "x": x, "y": y, "dx": dx, "dy": dy})
    
    def push_scroll(self, dx: float = 0, dy: float = 0):
        if dy:
            self._queue.append({"type": "scroll", "input": "scroll_y", "value": dy})
        if dx:
            self._queue.append({"type": "scroll", "input": "scroll_x", "value": dx})
    
    def pump(self):
        """Transfer events to handler in order."""
        while self._queue:
            e = self._queue.popleft()
            t = e.get("t") or e.get("timestamp")
            
            if e["type"] == "down":
                self.handler.feed_down(e["input"], e.get("x", self._cursor_x), 
                                       e.get("y", self._cursor_y), t)
            elif e["type"] == "up":
                self.handler.feed_up(e["input"], e.get("x", self._cursor_x),
                                     e.get("y", self._cursor_y), t)
            elif e["type"] == "move":
                x, y = e["x"], e["y"]
                dx, dy = e.get("dx", 0), e.get("dy", 0)
                self._cursor_x, self._cursor_y = x, y
                self.handler.feed_move(x, y, dx, dy, t)
            elif e["type"] == "scroll":
                self.handler.feed_change(e["input"], e.get("value", 0), t)

    @property
    def allow_hovering(self) -> bool:
        """True if ImGui allows hovering (not blocking input)."""
        return True

#
# # =============================================================================
# # Pygame Integration Example
# # =============================================================================
#
# class PygameBackend:
#     """
#     Backend for pygame.
#
#     Usage:
#         handler = InputHandler()
#         backend = PygameBackend(handler)
#
#         while running:
#             handler.begin_frame()
#             handler.register_hovered(...)
#
#             for event in pygame.event.get():
#                 backend.handle(event)
#
#             backend.pump()
#             events = handler.process_frame()
#     """
#
#     def __init__(self, handler: InputHandler):
#         self.handler = handler
#         self._json = JsonBackend(handler)
#
#         # Pygame button mapping
#         self._buttons = {
#             1: "left_mouse",
#             2: "middle_mouse",
#             3: "right_mouse",
#             4: "mouse_4",
#             5: "mouse_5",
#         }
#
#     def handle(self, event):
#         """Handle a pygame event."""
#         try:
#             import pygame
#         except ImportError:
#             return
#
#         if event.type == pygame.MOUSEMOTION:
#             self._json.push_move(event.pos[0], event.pos[1],
#                                 event.rel[0] if hasattr(event, 'rel') else None,
#                                 event.rel[1] if hasattr(event, 'rel') else None)
#
#         elif event.type == pygame.MOUSEBUTTONDOWN:
#             input_id = self._buttons.get(event.button, f"mouse_{event.button}")
#             self._json.push_down(input_id, event.pos[0], event.pos[1])
#
#         elif event.type == pygame.MOUSEBUTTONUP:
#             input_id = self._buttons.get(event.button, f"mouse_{event.button}")
#             self._json.push_up(input_id, event.pos[0], event.pos[1])
#
#         elif event.type == pygame.MOUSEWHEEL:
#             self._json.push_scroll(event.x, event.y)
#
#         elif event.type == pygame.KEYDOWN:
#             input_id = self._key_name(event)
#             self._json.push_down(input_id)
#             self._update_mods(event)
#
#         elif event.type == pygame.KEYUP:
#             input_id = self._key_name(event)
#             self._json.push_up(input_id)
#             self._update_mods(event)
#
#     def _key_name(self, event) -> str:
#
#         # Check for pygame
#         import pygame
#         name = pygame.key.name(event.key)
#
#         # Single letters
#         if len(name) == 1:
#             return name.lower()
#
#         # Normalize common names
#         mapping = {
#             "space": "space",
#             "return": "enter",
#             "escape": "escape",
#             "tab": "tab",
#             "backspace": "backspace",
#             "delete": "delete",
#             "left shift": "left_shift",
#             "right shift": "right_shift",
#             "left ctrl": "left_ctrl",
#             "right ctrl": "right_ctrl",
#             "left alt": "left_alt",
#             "right alt": "right_alt",
#             "left meta": "left_meta",
#             "right meta": "right_meta",
#         }
#
#         return mapping.get(name, name.lower().replace(" ", "_"))
#
#     def _update_mods(self, event):
#         import pygame
#         mods = pygame.key.get_mods()
#         self.handler.set_modifiers(
#             shift=bool(mods & pygame.KMOD_SHIFT),
#             ctrl=bool(mods & pygame.KMOD_CTRL),
#             alt=bool(mods & pygame.KMOD_ALT),
#             meta=bool(mods & pygame.KMOD_META),
#         )
#
#     def pump(self):
#         """Transfer events to handler."""
#         self._json.pump()
#
#     @property
#     def allow_hovering(self) -> bool:
#         """True if ImGui allows hovering (not blocking input)."""
#         return True
#


