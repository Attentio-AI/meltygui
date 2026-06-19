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


class EventAction:
    DOWN = "down"
    UP = "up"
    DRAGGED = "dragged"
    DRAG_RELEASED = "drag_released"
    # The drag variants refer on double-click: the SECOND press of a double-click,
    # held and dragged. Fires continuously (like DRAGGED) then once on release.
    DOUBLE_DRAGGED = "double_dragged"
    DOUBLE_DRAG_RELEASED = "double_drag_released"
    CLICKED = "clicked"
    DOUBLE_CLICKED = "double_clicked"
    CHANGED = "changed"
    MOVED = "moved"
    HOVERED = "hovered"  # Continuous - fires every frame while hovered
    HOVER_ENTER = "hover_enter"  # Once - when hover starts
    HOVER_EXIT = "hover_exit"  # Once - when hover ends
    HELD = "held"  # Continuous - fires every frame while down but within drag threshold


ACTION_ALIASES = {
    "pressed": EventAction.DOWN,
    "released": EventAction.UP,
    "drag": EventAction.DRAGGED,
    "drag_release": EventAction.DRAG_RELEASED,
    "double_drag": EventAction.DOUBLE_DRAGGED,
    "double_drag_release": EventAction.DOUBLE_DRAG_RELEASED,
    "click": EventAction.CLICKED,
    "double_click": EventAction.DOUBLE_CLICKED,
    # Continuous hover
    "hover": EventAction.HOVERED,
    "on_hover": EventAction.HOVERED,
    # Enter/exit
    "on_hover_enter": EventAction.HOVER_ENTER,
    "on_hover_exit": EventAction.HOVER_EXIT,
    "unhovered": EventAction.HOVER_EXIT,
    "unhover": EventAction.HOVER_EXIT,
    # Held (down within drag threshold)
    "hold": EventAction.HELD,
    "holding": EventAction.HELD,
    "on_hold": EventAction.HELD,
}

ALL_ACTIONS = frozenset({
    EventAction.DOWN, EventAction.UP, EventAction.DRAGGED, EventAction.DRAG_RELEASED,
    EventAction.DOUBLE_DRAGGED, EventAction.DOUBLE_DRAG_RELEASED, EventAction.CLICKED,
    EventAction.DOUBLE_CLICKED, EventAction.CHANGED, EventAction.MOVED,
    EventAction.HOVERED, EventAction.HOVER_ENTER, EventAction.HOVER_EXIT, EventAction.HELD,
    *ACTION_ALIASES.keys()
})
_SORTED_ACTIONS = tuple(sorted(ALL_ACTIONS, key=len, reverse=True))

# A "double" word anywhere in a subscription name promotes the base gesture to
# its double-press variant, so "double_right_mouse_drag" and the suffix form
# "right_mouse_double_drag" both canonicalise to DOUBLE_DRAGGED on right_mouse.
# Parsed like the inverted/non_blocking flags (see parse_event_name).
_DOUBLE_PROMOTE = {
    EventAction.DRAGGED: EventAction.DOUBLE_DRAGGED,
    EventAction.DRAG_RELEASED: EventAction.DOUBLE_DRAG_RELEASED,
    EventAction.CLICKED: EventAction.DOUBLE_CLICKED,
}

# Max gap between the two clicks' RELEASES. 0.1 was below human double-click
# speed (~150-300ms between releases; OS defaults ~500ms, imgui uses 300ms) -
# things like left_mouse_double_clicked() never fired.
DOUBLE_CLICK_WINDOW = 0.1
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
    # True when the current press is the SECOND down of a double-click (set in
    # feed_down). Lets a drag off this press dispatch as DOUBLE_DRAGGED.
    is_double_press: bool = False


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
    name, is_double = _strip_flag(name, "double")
    name, mods = _strip_mods(name)
    pfx = mod_prefix(mods)

    # Check if the name itself is an action (e.g., "hovered", "clicked")
    if name in ALL_ACTIONS:
        canonical = ACTION_ALIASES.get(name, name)
        if is_double:
            canonical = _DOUBLE_PROMOTE.get(canonical, canonical)
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
            if is_double:
                canonical = _DOUBLE_PROMOTE.get(canonical, canonical)
            result = (pfx + input_id, canonical, inverted, non_blocking)
            _parse_cache[original] = result
            return result

    # No action suffix (e.g., "ctrl_z", "left_mouse"): default to DOWN. Emitted
    # events always carry a concrete action, so an empty action would never match
    # and the subscription would silently never fire - a footgun. A bare key or
    # mouse name means "this went down".
    result = (pfx + name, EventAction.DOWN, inverted, non_blocking)
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
        '_down_origins', '_blocker_views', '_pending_clicks'
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
        self._drag_capture: dict[str, Any] = {}  # input_id -> (view_id, drag_action) captured on down
        self._drag_activated: dict[str, bool] = {}  # input_id -> whether drag threshold exceeded
        self._down_origins: dict[str, set] = {}  # input_id -> set of view_ids hovered at down time
        self._blocker_views: set = set()
        # CLICKED events held back to disambiguate single vs double, but ONLY for
        # inputs that have a double-click/double-drag subscriber hovered (so plain
        # clicks elsewhere keep zero latency). input_id -> (deadline, event,
        # [(view_id, key), ...] targets resolved at defer time). Flushed when the
        # double-click window expires with no double; cancelled when a 2nd press
        # (is_double_press) or a DOUBLE_CLICKED for that input arrives.
        self._pending_clicks: dict[str, tuple] = {}

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
                merged_priority = max(pri, priority)
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
        # A "double press" is the SECOND down of a double-click: it's a
        # recent click (click_count=1, within DOUBLE_CLICK_WINDOW of the last
        # release) landing near the prior press. Recorded so a drag off this
        # down dispatches as DOUBLE_DRAGGED. Distance is measured against the
        # previous down_x, so this must run before down_x/y are reset.
        dist = ((x - state.down_x) ** 2 + (y - state.down_y) ** 2) ** 0.5
        state.is_double_press = (
            state.click_count >= 1
            and (t - state.last_up_time) <= DOUBLE_CLICK_WINDOW
            and dist <= CLICK_MAX_DISTANCE
        )
        state.is_down = True
        state.down_time = t
        state.down_x = x
        state.down_y = y

        self._emit(input_id, EventAction.DOWN, x, y, t=t)

    def feed_up(self, input_id: str, x: float = None, y: float = None, t: float = None):
        t = t or time.perf_counter()
        x = self._cursor_x if x is None else x
        y = self._cursor_y if y is None else y

        state = self._state(input_id)
        was_down = state.is_down
        state.is_down = False

        self._emit(input_id, EventAction.UP, x, y, t=t)

        if was_down:
            dist = ((x - state.down_x) ** 2 + (y - state.down_y) ** 2) ** 0.5

            # Click if didn't move too far (no duration limit)
            if dist <= CLICK_MAX_DISTANCE:
                if t - state.last_up_time <= DOUBLE_CLICK_WINDOW:
                    state.click_count += 1
                    if state.click_count >= 2:
                        self._emit(input_id, EventAction.DOUBLE_CLICKED, x, y, t=t)
                        state.click_count = 0
                else:
                    state.click_count = 1
                self._emit(input_id, EventAction.CLICKED, x, y, t=t)
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

        self._emit("cursor", EventAction.MOVED, x, y, dx, dy, t=t)

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
            if e.input_id == input_id and e.action == EventAction.CHANGED:
                e.value += value
                if t is not None:
                    e.timestamp = t
                return
        self._emit(input_id, EventAction.CHANGED, self._cursor_x, self._cursor_y, value=value, t=t)

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
            tile_id = _view_id_to_tile_id.get(view_id, None)
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
        hover_enter_key = ("cursor", EventAction.HOVER_ENTER)
        hovered_key = ("cursor", EventAction.HOVERED)
        hover_exit_key = ("cursor", EventAction.HOVER_EXIT)

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
            add_event(v, hover_enter_key, InputEvent("cursor", None, EventAction.HOVER_ENTER, cx, cy, 0, 0, 0, t, mods))
        for v in hovered_views:
            add_event(v, hovered_key, InputEvent("cursor", None, EventAction.HOVERED, cx, cy, 0, 0, 0, t, mods))
        for v in exit_views:
            add_event(v, hover_exit_key, InputEvent("cursor", None, EventAction.HOVER_EXIT, cx, cy, 0, 0, 0, t, mods))

        # Update previous hover for next frame
        self._prev_hovered = current_hovered

        # --- Regular events ---
        # Hoist import once (sys.modules lookup still has overhead in a loop)
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        get_latest_mouse = Melty.get_latest_mouse

        drag_capture = self._drag_capture
        drag_activated = self._drag_activated
        down_origins = self._down_origins
        states = self._states
        pending_clicks = self._pending_clicks
        last_dx, last_dy = self._last_dx, self._last_dy

        # Inputs that completed a double-click THIS frame - their pending CLICKED
        # (emitted alongside the DOUBLE_CLICKED on the 2nd up) is absorbed.
        doubled_this_frame = {e.input_id for e in self._pending
                              if e.action == EventAction.DOUBLE_CLICKED}

        def _click_targets(ev):
            """Resolve (view_id, key) pairs a CLICKED would dispatch to NOW, so a
            deferred click replays to the same views regardless of later hover."""
            ck = (ev.input_id, EventAction.CLICKED)
            out = [(v, ck) for v in resolve(ck, key_index)]
            if ev.modifiers:
                mk = (mod_prefix(ev.modifiers) + ev.input_id, EventAction.CLICKED)
                if mk != ck:
                    out += [(v, mk) for v in resolve(mk, key_index)]
            return out

        for event in self._pending:
            key = (event.input_id, event.action)
            action = event.action

            # On DOWN, capture drag target and record origin views. A double-
            # press (the 2nd down of a double-click) prefers DOUBLE_DRAGGED
            # subscribers and only falls back to plain DRAGGED, so a double-drag
            # gesture still drags normally where nothing wants the double form.
            # The captured action is stored so the activation + release passes
            # emit the matching DRAGGED/DOUBLE_DRAGGED variant.
            if action == EventAction.DOWN:
                st = states.get(event.input_id)
                # A 2nd press (is_double_press) means a double interaction has
                # begun - double-click or double-drag. Either way the deferred
                # single click is irrelevant, so cancel it now.
                if st is not None and st.is_double_press:
                    pending_clicks.pop(event.input_id, None)
                drag_action = EventAction.DRAGGED
                capture_views = None
                if st is not None and st.is_double_press:
                    capture_views = resolve((event.input_id, EventAction.DOUBLE_DRAGGED), key_index)
                    if capture_views:
                        drag_action = EventAction.DOUBLE_DRAGGED
                if not capture_views:
                    capture_views = resolve((event.input_id, EventAction.DRAGGED), key_index)
                if capture_views:
                    drag_capture[event.input_id] = (capture_views[0], drag_action)
                    drag_activated[event.input_id] = False

                # Record all currently hovered views as origin for this input
                down_origins[event.input_id] = {v for v, _, _ in self._hovered}

            # On UP, emit drag_released only if drag was activated. The release
            # variant mirrors the captured drag variant (double-drag → double).
            elif action == EventAction.UP:
                cap = drag_capture.pop(event.input_id, None)
                was_activated = drag_activated.pop(event.input_id, False)
                down_origins.pop(event.input_id, None)
                if cap is not None and was_activated:
                    captured_view, drag_action = cap
                    rel_action = (EventAction.DOUBLE_DRAG_RELEASED
                                  if drag_action == EventAction.DOUBLE_DRAGGED
                                  else EventAction.DRAG_RELEASED)
                    drag_released_key = (event.input_id, rel_action)

                    lx, ly = get_latest_mouse()
                    state = states.get(event.input_id)
                    total_dx = lx - state.down_x if state else 0.0
                    total_dy = ly - state.down_y if state else 0.0

                    release_event = InputEvent(
                        event.input_id, rel_action, event.tile_id, lx, ly,
                        last_dx, last_dy, 0, t, mods, total_dx, total_dy
                    )
                    add_event(captured_view, drag_released_key, release_event)

                    if state:
                        state.down_x = 0.0
                        state.down_y = 0.0
                        state.down_time = 0.0

            # Single/double-click disambiguation. Only kicks in when a double
            # subscriber for this input is hovered - otherwise clicks dispatch
            # immediately (zero latency) as before.
            elif action == EventAction.CLICKED:
                X = event.input_id
                if X in doubled_this_frame:
                    # The 2nd click of a double - absorbed by the DOUBLE click.
                    pending_clicks.pop(X, None)
                    continue
                if (key_index.get((X, EventAction.DOUBLE_CLICKED))
                        or key_index.get((X, EventAction.DOUBLE_DRAGGED))):
                    # Hold this click until the double-click window ends; a 2nd
                    # press (handled in DOWN) cancels it, otherwise it flushes.
                    pending_clicks[X] = (event.timestamp + DOUBLE_CLICK_WINDOW,
                                         event, _click_targets(event))
                    continue

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
            held_key = (input_id, EventAction.HELD)
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
                        input_id, EventAction.HELD, tile_id, lx, ly,
                        last_dx, last_dy, 0, t, mods, total_dx, total_dy
                    )
                    add_event(v, held_key, held_event)

        # --- Continuous drag events (after threshold) ---
        # Emits the action captured on DOWN (DRAGGED or its DOUBLE_DRAGGED form).
        drag_threshold_sq = DRAG_THRESHOLD * DRAG_THRESHOLD
        for input_id, state in states.items():
            if not state.is_down:
                continue
            cap = drag_capture.get(input_id)
            if cap is None:
                continue
            captured_view, drag_action = cap

            lx, ly = get_latest_mouse()
            total_dx = lx - state.down_x
            total_dy = ly - state.down_y

            if not drag_activated.get(input_id, False):
                if total_dx * total_dx + total_dy * total_dy < drag_threshold_sq:
                    continue
                drag_activated[input_id] = True

            drag_key = (input_id, drag_action)
            tile_id = tile_cache_get(captured_view, None)
            drag_event = InputEvent(
                input_id, drag_action, tile_id, lx, ly,
                last_dx, last_dy, 0, t, mods, total_dx, total_dy
            )
            add_event(captured_view, drag_key, drag_event)

        # --- Flush pending clicks whose double-click window expired with no
        # double. Dispatch into this frame's result (melty: begin() then
        # invalidates the target tile so a cached view re-renders + consumes it).
        # While anything is still pending, keep the render loop alive so the
        # deadline is actually reached even if the app would otherwise idle. ----
        if pending_clicks:
            for X in [k for k, v in pending_clicks.items() if t >= v[0]]:
                _, ev, targets = pending_clicks.pop(X)
                for v, k in targets:
                    add_event(v, k, ev)
            if pending_clicks:
                request_render()

        return result, result_by_type

    def is_down(self, input_id: str) -> bool:
        s = self._states.get(input_id)
        return s.is_down if s else False

    def cursor(self) -> tuple[float, float]:
        return (self._cursor_x, self._cursor_y)