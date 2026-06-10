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
    HELD = "held"  # Continuous - fires every frame while down but within drag threshold


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
    # Held (down within drag threshold)
    "hold": Action.HELD,
    "holding": Action.HELD,
    "on_hold": Action.HELD,
}

ALL_ACTIONS = frozenset({
    Action.DOWN, Action.UP, Action.DRAGGED, Action.DRAG_RELEASED, Action.CLICKED,
    Action.DOUBLE_CLICKED, Action.CHANGED, Action.MOVED,
    Action.HOVERED, Action.HOVER_ENTER, Action.HOVER_EXIT, Action.HELD,
    *ACTION_ALIASES.keys()
})
_SORTED_ACTIONS = tuple(sorted(ALL_ACTIONS, key=len, reverse=True))

# Max gap between the two clicks' RELEASES. 0.1 was below human double-click
# speed (~150-300ms between releases; OS defaults ~500ms, imgui uses 300ms) -
# things like left_mouse_double_clicked() never fired.
DOUBLE_CLICK_WINDOW = 0.35
CLICK_MAX_DISTANCE = 5.0
DRAG_THRESHOLD = 2.0  # Minimum distance before drag activates


@dataclass(slots=True)
class InputEvent:
    input_id: str
    action: str
    tile_id: str = None
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
    def metadata(self) -> bool: return bool(self.modifiers & 8)


@dataclass(slots=True)
class _InputState:
    is_down: bool = False
    down_time: float = 0.0
    down_x: float = 0.0
    down_y: float = 0.0
    last_up_time: float = 0.0
    click_count: int = 0


_parse_cache: dict[str, tuple[str, str, bool, bool]] = {}  # (input_id, action, inverted, non_blocking)
_view_id_names_cache: dict[Any, dict[tuple[str, str], str]] = {}
_view_id_flags_cache: dict[
    Any, dict[tuple[str, str], tuple[bool, bool]]] = {}  # view_id, key -> (inverted, non_blocking)
_view_id_to_tile_id: dict[str, str] = {}


def _strip_flag(name: str, flag: str) -> tuple[str, bool]:
    """Strip a flag word from anywhere in an underscore-delimited name."""
    prefix = flag + "_"
    infix = "_" + flag + "_"
    suffix = "_" + flag
    if name.startswith(prefix):
        return name[len(prefix):], True
    if infix in name:
        return name.replace(infix, "_", 1), True
    if name.endswith(suffix):
        return name[:-len(suffix)], True
    return name, False


# Keyboard-modifier words and their bit (matching InputHandler.set_modifiers:
# shift=1, ctrl=2, alt=4, meta=8). Emitted/parsed in a fixed order so
# "ctrl_shift_f" and "shift_ctrl_f" canonicalise to the same input_id.
_MOD_WORDS = (("ctrl", 2), ("shift", 1), ("alt", 4), ("meta", 8))


def _strip_mods(name: str) -> tuple[str, int]:
    """Strip modifier words (ctrl/shift/alt/meta) from `name`, returning the
    remainder and the combined modifier mask."""
    mask = 0
    for word, bit in _MOD_WORDS:
        name, found = _strip_flag(name, word)
        if found:
            mask |= bit
    return name, mask


def mod_prefix(mods: int) -> str:
    """The canonical "ctrl_shift_…" prefix for a modifier mask (fixed order)."""
    return "".join(f"{word}_" for word, bit in _MOD_WORDS if mods & bit)


def parse_event_name(name: str) -> tuple[str, str, bool, bool]:
    """Parse "left_mouse_up" → ("left_mouse", "up", False, False)
    Parse "inverted_left_mouse_clicked" → ("left_mouse", "clicked", True, False)
    Parse "non_blocking_left_mouse_clicked" → ("left_mouse", "clicked", False, True)
    Parse "ctrl_shift_f_key_down" → ("ctrl_shift_f", "down", False, False) — the
    modifier words are folded (canonically ordered) into the input_id, so a
    modified shortcut dispatches in its own bucket instead of sharing the plain
    key's and being contended/blocked by other subscribers.
    """
    if name in _parse_cache:
        return _parse_cache[name]

    original = name
    if name.startswith("on_"):
        name = name[3:]

    # Strip flags, then modifier words (folded back into input_id prefix).
    name, inverted = _strip_flag(name, "inverted")
    name, non_blocking = _strip_flag(name, "non_blocking")
    if not non_blocking:
        name, non_blocking = _strip_flag(name, "nonblocking")
    name, mods = _strip_mods(name)
    pfx = mod_prefix(mods)

    # Check if the name itself is an action (e.g., "hovered", "clicked")
    if name in ALL_ACTIONS:
        canonical = ACTION_ALIASES.get(name, name)
        result = (pfx + "cursor", canonical, inverted, non_blocking)
        _parse_cache[original] = result
        return result

    # Check for action suffix
    for action in _SORTED_ACTIONS:
        if name.endswith(f"_{action}"):
            input_id = name[:-(len(action) + 1)]
            if input_id.endswith("_key"):
                input_id = input_id[:-4]
            canonical = ACTION_ALIASES.get(action, action)
            result = (pfx + input_id, canonical, inverted, non_blocking)
            _parse_cache[original] = result
            return result

    # No action suffix (e.g., "ctrl_z", "left_mouse"): default to DOWN. Emitted
    # events always carry a concrete action, so an empty action would never match
    # and the subscription would silently never fire - a footgun. A bare key or
    # mouse name means "this went down".
    result = (pfx + name, Action.DOWN, inverted, non_blocking)
    _parse_cache[original] = result
    return result


class InputHandler:
    """
    Usage:
        handler = InputHandler()

        handler.begin_frame()
        handler.register_hovered("btn", ["left_mouse_clicked"])
        handler.register_hovered("panel", ["left_mouse_dragged"], priority=1)

        # Inverted priority (parent/root views fire first):
        handler.register_hovered("root", ["inverted_left_mouse_clicked"])

        # Feed from backend
        handler.feed_down("left_mouse", x, y)
        handler.feed_move(x, y)
        handler.feed_up("left_mouse", x, y)

        events = handler.process_frame()
        # {"btn": {"left_mouse_clicked": InputEvent(...)}, ...}
    """

    __slots__ = (
        '_states', '_hovered', '_prev_hovered', '_pending', '_cursor_x', '_cursor_y',
        '_modifiers', '_last_dx', '_last_dy', '_drag_capture', '_drag_activated',
        '_down_origins', '_blocker_views'
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
        self._drag_activated: dict[str, bool] = {}  # input_id -> whether drag threshold exceeded
        self._down_origins: dict[str, set] = {}  # input_id -> set of view_ids hovered at down time
        self._blocker_views: set = set()

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
        self._blocker_views.clear()

    def register_hovered(self, view_id: Any, subscribed: list[str], priority: int = 0, tile_id=None, selected=False, blocker=False):
        """Register hovered view. Priority 0 = topmost.

        Multiple calls with the same view_id will merge subscriptions,
        using the lowest (best) priority.

        Including "inverted" in a subscription name (e.g. "inverted_left_mouse_clicked")
        causes that event to fire to the highest-priority-number (parent/root) view first,
        reversing the normal child-first dispatch order.

        blocker=True makes this view consume all events at its priority level.
        Views with a higher priority number (lower priority) than the topmost
        blocker will not receive any events. Inverted events are scoped to
        within the blocker boundary.
        """
        if blocker:
            self._blocker_views.add(view_id)

        # Parse new subscriptions
        new_subs = set()
        for s in subscribed:
            if selected and s == "scroll_y_changed":
                priority -= 20
            input_id, action, inverted, non_blocking = parse_event_name(s)
            sub = (input_id, action)
            if view_id not in _view_id_names_cache:
                _view_id_names_cache[view_id] = {}
                _view_id_flags_cache[view_id] = {}
            _view_id_names_cache[view_id][sub] = s
            _view_id_flags_cache[view_id][sub] = (inverted, non_blocking)
            _view_id_to_tile_id[view_id] = tile_id
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
        tile_id = _view_id_to_tile_id.get(input_id, None)
        self._pending.append(InputEvent(
            input_id, action, tile_id, x, y, dx, dy, value,
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
        # Coalesce repeated CHANGED events for the same input within a frame by
        # summing their values. Scroll-wheel notches arrive as separate
        # callbacks; when the framerate drops, many come between two
        # process_frame() calls. Dispatch keys events by name and overwrites
        # (add_event: vdict[event_name] = event), so without coalescing only the
        # last notch's delta would persist and the rest of the scroll distance
        # would be lost. Summing carries the accumulated scroll delta intact, the
        # same way Melty.frame_key_events preserves every keystroke under load.
        for e in self._pending:
            if e.input_id == input_id and e.action == Action.CHANGED:
                e.value += value
                if t is not None:
                    e.timestamp = t
                return
        self._emit(input_id, Action.CHANGED, self._cursor_x, self._cursor_y, value=value, t=t)

    @staticmethod
    def _resolve_subscribers(
            key: tuple[str, str],
            index: dict[tuple[str, str], list[tuple[Any, int]]],
    ) -> list[Any]:
        """Resolve subscriber chain from precomputed index.

        Normal: lowest priority first (child-first).
        Inverted: highest priority first (parent-first).
        Non-blocking: collects multiple views until a blocking one.
        """
        subscribers = index.get(key)
        if not subscribers:
            return ()

        # Single subscriber fast path (most common case)
        if len(subscribers) == 1:
            return (subscribers[0][0],)

        # Check flags
        any_inverted = False
        any_non_blocking = False
        flags_cache_get = _view_id_flags_cache.get
        for v, _ in subscribers:
            flags = flags_cache_get(v)
            if flags:
                f = flags.get(key)
                if f:
                    any_inverted = any_inverted or f[0]
                    any_non_blocking = any_non_blocking or f[1]
                    if any_inverted and any_non_blocking:
                        break

        # If no special flags, first subscriber wins (already sorted by priority asc)
        if not any_inverted and not any_non_blocking:
            return (subscribers[0][0],)

        # Need to reorder or walk chain
        ordered = subscribers
        if any_inverted:
            ordered = sorted(subscribers, key=lambda x: x[1], reverse=True)

        if not any_non_blocking:
            return (ordered[0][0],)

        # Walk non-blocking chain
        result = []
        for v, _ in ordered:
            result.append(v)
            flags = flags_cache_get(v)
            if flags:
                f = flags.get(key)
                if not f or not f[1]:
                    break
            else:
                break
        return result

    def process_frame(self):
        """Returns {view_id: {event_name: event}} for all matched subscriptions."""
        self._hovered.sort(key=lambda x: x[1])

        # --- Blocker: drop views below the topmost blocker ---
        # A blocker (closable window) stops events reaching anything stacked
        # below it. Exception: non_blocking subscriptions survive - they're
        # pass-through global handlers (e.g. an app-wide shortcut on the root),
        # which shouldn't be swallowed just because a window is in front. Such a
        # non-blocker view is kept, but only its non_blocking subs.
        if self._blocker_views:
            blocker_priority = None
            blocker_tile = None
            for view_id, priority, subs in self._hovered:
                if view_id in self._blocker_views:
                    blocker_priority = priority
                    blocker_tile = _view_id_to_tile_id.get(view_id)
                    break  # list is sorted asc, first match is topmost
            if blocker_priority is not None:
                flags_get = _view_id_flags_cache.get
                kept = []
                for v, p, s in self._hovered:
                    if p <= blocker_priority or _view_id_to_tile_id.get(v) == blocker_tile:
                        kept.append((v, p, s))
                        continue
                    vf = flags_get(v)
                    if vf:
                        passthrough = frozenset(sub for sub in s if vf.get(sub, (False, False))[1])
                        if passthrough:
                            kept.append((v, p, passthrough))
                self._hovered = kept

        # --- Precompute key → [(view_id, priority)] index (sorted by priority asc) ---
        key_index: dict[tuple[str, str], list[tuple[Any, int]]] = {}
        for view_id, priority, subs in self._hovered:
            vp = (view_id, priority)
            for key in subs:
                bucket = key_index.get(key)
                if bucket is None:
                    key_index[key] = [vp]
                else:
                    bucket.append(vp)

        resolve = self._resolve_subscribers
        result: dict[Any, dict[str, InputEvent]] = {}
        result_by_type: dict[Any, dict[str, InputEvent]] = {}
        t = time.perf_counter()

        # Build current hover dict with priorities
        current_hovered: dict[Any, tuple[int, frozenset]] = {
            view_id: (priority, subs) for view_id, priority, subs in self._hovered
        }

        # Bind frequently-used lookups to locals
        names_cache_get = _view_id_names_cache.get
        tile_cache_get = _view_id_to_tile_id.get
        cx, cy = self._cursor_x, self._cursor_y
        mods = self._modifiers

        def add_event(view_id: Any, key: tuple[str, str], event: InputEvent):
            cache = names_cache_get(view_id)
            if cache is None:
                return
            event_name = cache.get(key)
            if event_name is None:
                return
            vdict = result.get(view_id)
            if vdict is None:
                vdict = {}
                result[view_id] = vdict
            tdict = result_by_type.get(event_name)
            if tdict is None:
                tdict = {}
                result_by_type[event_name] = tdict
            vdict[event_name] = event
            tdict[view_id] = event

        # --- Hover events ---
        hover_enter_key = ("cursor", Action.HOVER_ENTER)
        hovered_key = ("cursor", Action.HOVERED)
        hover_exit_key = ("cursor", Action.HOVER_EXIT)

        # Build index for newly-entered views (for enter events)
        prev_hovered = self._prev_hovered
        newly_index: dict[tuple[str, str], list[tuple[Any, int]]] = {}
        for view_id, priority, subs in self._hovered:
            if view_id not in prev_hovered:
                vp = (view_id, priority)
                for key in subs:
                    bucket = newly_index.get(key)
                    if bucket is None:
                        newly_index[key] = [vp]
                    else:
                        bucket.append(vp)

        enter_views = resolve(hover_enter_key, newly_index)
        hovered_views = resolve(hovered_key, key_index)

        # Build index for exited views
        exit_index: dict[tuple[str, str], list[tuple[Any, int]]] = {}
        for v, (p, subs) in prev_hovered.items():
            if v not in current_hovered:
                vp = (v, p)
                for key in subs:
                    bucket = exit_index.get(key)
                    if bucket is None:
                        exit_index[key] = [vp]
                    else:
                        bucket.append(vp)
        # Sort exit buckets by priority
        for bucket in exit_index.values():
            if len(bucket) > 1:
                bucket.sort(key=lambda x: x[1])
        exit_views = resolve(hover_exit_key, exit_index)

        # Emit hover events
        for v in enter_views:
            add_event(v, hover_enter_key, InputEvent("cursor", None, Action.HOVER_ENTER, cx, cy, 0, 0, 0, t, mods))
        for v in hovered_views:
            add_event(v, hovered_key, InputEvent("cursor", None, Action.HOVERED, cx, cy, 0, 0, 0, t, mods))
        for v in exit_views:
            add_event(v, hover_exit_key, InputEvent("cursor", None, Action.HOVER_EXIT, cx, cy, 0, 0, 0, t, mods))

        # Update previous hover for next frame
        self._prev_hovered = current_hovered

        # --- Regular events ---
        # Hoist import once (sys.modules lookup still has overhead in a loop)
        from src.lsd.gl_gui.melty import Melty
        get_latest_mouse = Melty.get_latest_mouse

        drag_capture = self._drag_capture
        drag_activated = self._drag_activated
        down_origins = self._down_origins
        states = self._states
        last_dx, last_dy = self._last_dx, self._last_dy

        for event in self._pending:
            key = (event.input_id, event.action)
            action = event.action

            # On DOWN, capture drag view and record origin views
            if action == Action.DOWN:
                drag_key = (event.input_id, Action.DRAGGED)
                capture_views = resolve(drag_key, key_index)
                if capture_views:
                    drag_capture[event.input_id] = capture_views[0]
                    drag_activated[event.input_id] = False

                # Record all currently hovered views as origin for this input
                down_origins[event.input_id] = {v for v, _, _ in self._hovered}

            # On UP, emit drag_released only if drag was active
            elif action == Action.UP:
                captured_view = drag_capture.pop(event.input_id, None)
                was_activated = drag_activated.pop(event.input_id, False)
                down_origins.pop(event.input_id, None)
                if captured_view is not None and was_activated:
                    drag_released_key = (event.input_id, Action.DRAG_RELEASED)

                    lx, ly = get_latest_mouse()
                    state = states.get(event.input_id)
                    total_dx = lx - state.down_x if state else 0.0
                    total_dy = ly - state.down_y if state else 0.0

                    release_event = InputEvent(
                        event.input_id, Action.DRAG_RELEASED, event.tile_id, lx, ly,
                        last_dx, last_dy, 0, t, mods, total_dx, total_dy
                    )
                    add_event(captured_view, drag_released_key, release_event)

                    if state:
                        state.down_x = 0.0
                        state.down_y = 0.0
                        state.down_time = 0.0

            for v in resolve(key, key_index):
                add_event(v, key, event)

            # Modifier-qualified subscribers (e.g. "ctrl_shift_f_down") live in
            # their own bucket keyed by a "ctrl_shift_..."-prefixed input ID, so
            # resolve that too when modifiers are held. The plain bucket above
            # still fires (legacy "fire on any mods" behaviour), so a view can
            # subscribe either way.
            if event.modifiers:
                mkey = (mod_prefix(event.modifiers) + event.input_id, event.action)
                if mkey != key:
                    for v in resolve(mkey, key_index):
                        add_event(v, mkey, event)

        # --- Continuous held events (only to views hovered at DOWN time) ---
        for input_id, state in states.items():
            if not state.is_down:
                continue
            origins = down_origins.get(input_id)
            if not origins:
                continue
            held_key = (input_id, Action.HELD)
            held_subs = resolve(held_key, key_index)
            if held_subs:
                lx, ly = get_latest_mouse()
                total_dx = lx - state.down_x
                total_dy = ly - state.down_y
                for v in held_subs:
                    if v not in origins:
                        continue
                    tile_id = tile_cache_get(v, None)
                    held_event = InputEvent(
                        input_id, Action.HELD, tile_id, lx, ly,
                        last_dx, last_dy, 0, t, mods, total_dx, total_dy
                    )
                    add_event(v, held_key, held_event)

        # --- Continuous drag events (after threshold) ---
        drag_threshold_sq = DRAG_THRESHOLD * DRAG_THRESHOLD
        for input_id, state in states.items():
            if not state.is_down:
                continue
            captured_view = drag_capture.get(input_id)
            if captured_view is None:
                continue

            lx, ly = get_latest_mouse()
            total_dx = lx - state.down_x
            total_dy = ly - state.down_y

            if not drag_activated.get(input_id, False):
                if total_dx * total_dx + total_dy * total_dy < drag_threshold_sq:
                    continue
                drag_activated[input_id] = True

            drag_key = (input_id, Action.DRAGGED)
            tile_id = tile_cache_get(captured_view, None)
            drag_event = InputEvent(
                input_id, Action.DRAGGED, tile_id, lx, ly,
                last_dx, last_dy, 0, t, mods, total_dx, total_dy
            )
            add_event(captured_view, drag_key, drag_event)

        return result, result_by_type

    def is_down(self, input_id: str) -> bool:
        s = self._states.get(input_id)
        return s.is_down if s else False

    def cursor(self) -> tuple[float, float]:
        return (self._cursor_x, self._cursor_y)