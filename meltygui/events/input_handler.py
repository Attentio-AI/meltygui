"""
Low-latency input event handler.

No callbacks - single method returns {view_id: [events]}.
Device-agnostic actions auto-parsed from subscription names.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import time

import imgui


class Action:
    DOWN = "down"
    UP = "up"
    DRAGGED = "dragged"
    DRAG_RELEASED = "drag_released"
    CLICKED = "clicked"
    DOUBLE_CLICKED = "double_clicked"
    CHANGED = "changed"
    MOVED = "moved"
    HOVERED = "hovered"  # Continuous - fires every frame while hovered
    HOVER_ENTER = "hover_enter"  # Once - when hover starts
    HOVER_EXIT = "hover_exit"  # Once - when hover ends


ACTION_ALIASES = {
    "pressed": Action.DOWN,
    "released": Action.UP,
    "drag": Action.DRAGGED,
    "drag_release": Action.DRAG_RELEASED,
    "click": Action.CLICKED,
    "double_click": Action.DOUBLE_CLICKED,
    # Continuous hover
    "hover": Action.HOVERED,
    "on_hover": Action.HOVERED,
    # Enter/exit
    "on_hover_enter": Action.HOVER_ENTER,
    "on_hover_exit": Action.HOVER_EXIT,
    "unhovered": Action.HOVER_EXIT,
    "unhover": Action.HOVER_EXIT,
}

ALL_ACTIONS = frozenset({
    Action.DOWN, Action.UP, Action.DRAGGED, Action.DRAG_RELEASED, Action.CLICKED,
    Action.DOUBLE_CLICKED, Action.CHANGED, Action.MOVED,
    Action.HOVERED, Action.HOVER_ENTER, Action.HOVER_EXIT,
    *ACTION_ALIASES.keys()
})
_SORTED_ACTIONS = tuple(sorted(ALL_ACTIONS, key=len, reverse=True))

DOUBLE_CLICK_WINDOW = 0.3
CLICK_MAX_DISTANCE = 5.0


@dataclass(slots=True)
class InputEvent:
    input_id: str
    action: str
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    value: float = 0.0
    timestamp: float = 0.0
    modifiers: int = 0
    total_dx: float = 0.0
    total_dy: float = 0.0

    @property
    def shift(self) -> bool: return bool(self.modifiers & 1)

    @property
    def ctrl(self) -> bool: return bool(self.modifiers & 2)

    @property
    def alt(self) -> bool: return bool(self.modifiers & 4)

    @property
    def meta(self) -> bool: return bool(self.modifiers & 8)


@dataclass(slots=True)
class _InputState:
    is_down: bool = False
    down_time: float = 0.0
    down_x: float = 0.0
    down_y: float = 0.0
    last_up_time: float = 0.0
    click_count: int = 0


_parse_cache: dict[str, tuple[str, str]] = {}
_view_id_names_cache: dict[Any, dict[tuple[str, str], str]] = {}


def parse_event_name(name: str) -> tuple[str, str]:
    """Parse "left_mouse_up" → ("left_mouse", "up")"""
    if name in _parse_cache:
        return _parse_cache[name]

    original = name
    if name.startswith("on_"):
        name = name[3:]

    # Check if the name itself is an action (e.g., "hovered", "clicked")
    if name in ALL_ACTIONS:
        canonical = ACTION_ALIASES.get(name, name)
        _parse_cache[original] = ("cursor", canonical)
        return ("cursor", canonical)

    # Check for action suffix
    for action in _SORTED_ACTIONS:
        if name.endswith(f"_{action}"):
            input_id = name[:-(len(action) + 1)]
            if input_id.endswith("_key"):
                input_id = input_id[:-4]
            canonical = ACTION_ALIASES.get(action, action)
            _parse_cache[original] = (input_id, canonical)
            return (input_id, canonical)

    _parse_cache[original] = (name, "")
    return (name, "")


class InputHandler:
    """
    Usage:
        handler = InputHandler()

        handler.begin_frame()
        handler.register_hovered("btn", ["left_mouse_clicked"])
        handler.register_hovered("panel", ["left_mouse_dragged"], priority=1)

        # Feed from backend
        handler.feed_down("left_mouse", x, y)
        handler.feed_move(x, y)
        handler.feed_up("left_mouse", x, y)

        events = handler.process_frame()
        # {"btn": {"left_mouse_clicked": InputEvent(...)}, ...}
    """

    __slots__ = (
        '_states', '_hovered', '_prev_hovered', '_pending', '_cursor_x', '_cursor_y',
        '_modifiers', '_last_dx', '_last_dy', '_drag_capture'
    )

    def __init__(self):
        self._states: dict[str, _InputState] = {}
        self._hovered: list[tuple[Any, int, frozenset]] = []
        self._prev_hovered: dict[Any, tuple[int, frozenset]] = {}  # view_id → (priority, subscriptions)
        self._pending: list[InputEvent] = []
        self._cursor_x = 0.0
        self._cursor_y = 0.0
        self._modifiers = 0
        self._last_dx = 0.0
        self._last_dy = 0.0
        self._drag_capture: dict[str, Any] = {}  # input_id -> view_id that captured it on down

    def _state(self, input_id: str) -> _InputState:
        s = self._states.get(input_id)
        if s is None:
            s = _InputState()
            self._states[input_id] = s
        return s

    def set_modifiers(self, shift=False, ctrl=False, alt=False, meta=False):
        self._modifiers = (shift and 1) | (ctrl and 2) | (alt and 4) | (meta and 8)

    def begin_frame(self):
        self._hovered.clear()
        self._pending.clear()
        self._last_dx = 0.0
        self._last_dy = 0.0

    def register_hovered(self, view_id: Any, subscribed: list[str], priority: int = 0):
        """Register hovered view. Priority 0 = topmost.

        Multiple calls with the same view_id will merge subscriptions,
        using the lowest (best) priority.
        """
        # Parse new subscriptions
        new_subs = set()
        for s in subscribed:
            sub = parse_event_name(s)
            if view_id not in _view_id_names_cache:
                _view_id_names_cache[view_id] = {}
            _view_id_names_cache[view_id][sub] = s
            new_subs.add(sub)

        # Check if view already registered this frame - merge if so
        for i, (vid, pri, subs) in enumerate(self._hovered):
            if vid == view_id:
                # Merge subscriptions, keep lowest priority
                merged_subs = subs | frozenset(new_subs)
                merged_priority = min(pri, priority)
                self._hovered[i] = (view_id, merged_priority, merged_subs)
                return

        # New view
        self._hovered.append((view_id, priority, frozenset(new_subs)))

    def _emit(self, input_id: str, action: str, x: float, y: float,
              dx: float = 0, dy: float = 0, value: float = 0, t: float = None):
        self._pending.append(InputEvent(
            input_id, action, x, y, dx, dy, value,
            t or time.perf_counter(), self._modifiers
        ))

    def feed_down(self, input_id: str, x: float = None, y: float = None, t: float = None):
        t = t or time.perf_counter()
        x = self._cursor_x if x is None else x
        y = self._cursor_y if y is None else y

        state = self._state(input_id)
        state.is_down = True
        state.down_time = t
        state.down_x = x
        state.down_y = y

        self._emit(input_id, Action.DOWN, x, y, t=t)

    def feed_up(self, input_id: str, x: float = None, y: float = None, t: float = None):
        t = t or time.perf_counter()
        x = self._cursor_x if x is None else x
        y = self._cursor_y if y is None else y

        state = self._state(input_id)
        was_down = state.is_down
        state.is_down = False

        self._emit(input_id, Action.UP, x, y, t=t)

        if was_down:
            dist = ((x - state.down_x) ** 2 + (y - state.down_y) ** 2) ** 0.5

            # Click if didn't move too far (no duration limit)
            if dist <= CLICK_MAX_DISTANCE:
                if t - state.last_up_time <= DOUBLE_CLICK_WINDOW:
                    state.click_count += 1
                    if state.click_count >= 2:
                        self._emit(input_id, Action.DOUBLE_CLICKED, x, y, t=t)
                        state.click_count = 0
                else:
                    state.click_count = 1
                self._emit(input_id, Action.CLICKED, x, y, t=t)
            else:
                state.click_count = 0

        state.last_up_time = t

    def feed_move(self, x: float, y: float, dx: float = None, dy: float = None, t: float = None):
        t = t or time.perf_counter()
        dx = x - self._cursor_x if dx is None else dx
        dy = y - self._cursor_y if dy is None else dy
        self._cursor_x, self._cursor_y = x, y
        self._last_dx = dx
        self._last_dy = dy

        self._emit("cursor", Action.MOVED, x, y, dx, dy, t=t)

    def feed_change(self, input_id: str, value: float, t: float = None):
        self._emit(input_id, Action.CHANGED, self._cursor_x, self._cursor_y, value=value, t=t)

    def process_frame(self) -> dict[Any, dict[str, InputEvent]]:
        """Returns {view_id: {event_name: event}} for all matched subscriptions."""
        self._hovered.sort(key=lambda x: x[1])

        result: dict[Any, dict[str, InputEvent]] = {}
        t = time.perf_counter()

        # Build current hover dict with priorities
        current_hovered: dict[Any, tuple[int, frozenset]] = {
            view_id: (priority, subs) for view_id, priority, subs in self._hovered
        }

        hover_enter_key = ("cursor", Action.HOVER_ENTER)
        hovered_key = ("cursor", Action.HOVERED)
        hover_exit_key = ("cursor", Action.HOVER_EXIT)

        def add_event(view_id: Any, key: tuple[str, str], event: InputEvent):
            """Safely add event to result using cached subscription name."""
            if view_id is None:
                return
            cache = _view_id_names_cache.get(view_id)
            if cache is None:
                return
            event_name = cache.get(key)
            if event_name is None:
                return
            if view_id not in result:
                result[view_id] = {}
            result[view_id][event_name] = event

        # Find top subscriber for each hover event type
        top_enter = next((v for v, _, s in self._hovered if v not in self._prev_hovered and hover_enter_key in s), None)
        top_hovered = next((v for v, _, s in self._hovered if hovered_key in s), None)

        # For exit, sort exited views by their stored priority
        exited = sorted(
            ((v, p, s) for v, (p, s) in self._prev_hovered.items() if v not in current_hovered),
            key=lambda x: x[1]
        )
        top_exit = next((v for v, _, s in exited if hover_exit_key in s), None)

        # Emit hover events
        def make_hover_event(action: str) -> InputEvent:
            return InputEvent("cursor", action, self._cursor_x, self._cursor_y, 0, 0, 0, t, self._modifiers)

        add_event(top_enter, hover_enter_key, make_hover_event(Action.HOVER_ENTER))
        add_event(top_hovered, hovered_key, make_hover_event(Action.HOVERED))
        add_event(top_exit, hover_exit_key, make_hover_event(Action.HOVER_EXIT))

        # Update previous hover for next frame
        self._prev_hovered = current_hovered

        # Process regular events: top priority subscriber gets each event
        for event in self._pending:
            key = (event.input_id, event.action)

            # On DOWN, capture drag target (top hovered view subscribed to drag)
            if event.action == Action.DOWN:
                drag_key = (event.input_id, Action.DRAGGED)
                capture_view = next((v for v, _, s in self._hovered if drag_key in s), None)
                if capture_view is not None:
                    self._drag_capture[event.input_id] = capture_view

            # On UP, emit drag_released to captured view, then release capture
            elif event.action == Action.UP:
                captured_view = self._drag_capture.pop(event.input_id, None)
                if captured_view is not None:
                    drag_released_key = (event.input_id, Action.DRAG_RELEASED)

                    from src.lsd.gl_gui.melty import Melty
                    lx, ly = Melty.get_latest_mouse()
                    state = self._states.get(event.input_id)
                    total_dx = lx - state.down_x if state else 0.0
                    total_dy = ly - state.down_y if state else 0.0

                    release_event = InputEvent(
                        event.input_id, Action.DRAG_RELEASED, lx, ly,
                        self._last_dx, self._last_dy, 0, t, self._modifiers, total_dx, total_dy
                    )
                    add_event(captured_view, drag_released_key, release_event)

                    # Reset initial drag state
                    if state:
                        state.down_x = 0.0
                        state.down_y = 0.0
                        state.down_time = 0.0

            top = next((v for v, _, s in self._hovered if key in s), None)
            add_event(top, key, event)

        # Emit continuous drag events only to captured views
        for input_id, state in self._states.items():
            if state.is_down:
                captured_view = self._drag_capture.get(input_id)
                if captured_view is not None:
                    drag_key = (input_id, Action.DRAGGED)

                    # JIT: get latest mouse position right before dispatch
                    from src.lsd.gl_gui.melty import Melty
                    lx, ly = Melty.get_latest_mouse()

                    total_dx = lx - state.down_x
                    total_dy = ly - state.down_y

                    drag_event = InputEvent(
                        input_id, Action.DRAGGED, lx, ly,
                        self._last_dx, self._last_dy, 0, t, self._modifiers, total_dx, total_dy
                    )
                    add_event(captured_view, drag_key, drag_event)

        return result

    def is_down(self, input_id: str) -> bool:
        s = self._states.get(input_id)
        return s.is_down if s else False

    def cursor(self) -> tuple[float, float]:
        return (self._cursor_x, self._cursor_y)