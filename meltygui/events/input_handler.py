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

# Max gap between the two clicks' RELEASES. Must clear human double-click speed
# (~150-300ms between releases; OS defaults ~500ms, imgui uses 300ms) or doubles
# never register - at 0.1 left_mouse_double_clicked (voxel params panel) never
# fired and right double-clicks fell over as two sloppy singles. This value
# ALSO sets how long a deferred single click waits before firing (only on inputs
# with a double subscriber - see process_frame), so it's the single↔double
# trade-off here: lower = snappier single click but flakier double detection.
DOUBLE_CLICK_WINDOW = 0.25
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
    # Multi-axis payload (feed_axes): the 6-DOF reading of a 3D mouse as
    # (tx, ty, tz, rx, ry, rz), each axis the deflection INTEGRATED over the
    # frame in full-deflection-terms (see events/space_mouse.py). None for
    # every single-value event; `value` stays 0 for an axes event.
    axes: tuple = None

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
    # True when this press is a CHORD - a mouse button pressed while the other
    # button is still held (see feed_down). A chorded press is level state
    # only: is_down() reads it, nothing is dispatched for it (no DOWN, no drag
    # events, no HELD, no CLICKED/UP on release). Panel window resize reads
    # is_down("left_mouse") during a right-drag to pick the corner.
    chord: bool = False


# Missed-release safety (InputHandler._reconcile_held). The backend installs
# a probe `fn(input_id) -> bool | None` via set_button_probe: True/False =
# the button's REAL level state, None = unknown (not a platform guess, no
# platform truth). Module-level and hotswap-survive because the handler
# instance (Melty.event_handler) outlives both hotswaps and studio restarts.
_BUTTON_PROBE: dict = globals().get("_BUTTON_PROBE") or {"fn": None}


def set_button_probe(fn):
    _BUTTON_PROBE["fn"] = fn


# Orchestrator record/replay funnel. Every REAL input reaches the handler
# through the feed_* methods below (the GLFW backend's callbacks and its
# per-frame cursor sample), so this one tap taps the whole stream: the
# Orchestrator (view/playback/orchestrator.py) registers
# `fn(kind, *args) -> bool` here - it records the event while a recording is
# armed, and returns True to CONSUME the real event while a replay is
# driving (the mute that keeps a stray real click from corrupting the replay).
# The key/char callbacks in event_backends.py call the input tap for the
# events that bypass the handler (Melty.frame_key_events and imgui events).
# Module-level and hotswap-surviving, same shape as _BUTTON_PROBE.
_INPUT_TAP: dict = globals().get("_INPUT_TAP") or {"fn": None}


def set_input_tap(fn):
    _INPUT_TAP["fn"] = fn


def input_tap(kind, *args):
    fn = _INPUT_TAP["fn"]
    if fn is None:
        return False
    try:
        return bool(fn(kind, *args))
    except Exception:
        return False


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
        '_down_origins', '_blocker_views', '_pending_clicks',
        '_view_cursor', '_drag_cursor', 'cursor_shape'
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
        # Mouse-cursor shapes (see gl_gui/mouse_cursor.py). view_id -> shape
        # registered this frame via register_hovered(cursor=); input_id ->
        # the shape that was SHOWING when that input's drag was captured
        # (sticky until release); and this frame's resolved shape (None =
        # nothing asked, the last shape).
        self._view_cursor: dict[Any, tuple] = {}   # view_id -> (shape, rect | None)
        self._drag_cursor: dict[str, Any] = {}
        self.cursor_shape = None

    def _reconcile_held(self, t: float):
        """Drop any press the handler still holds that the platform says is
        UP. is_down only ever clears through feed_up, and a RELEASE can be
        lost — a freeze (the compositor breaks the implicit grab and hands the
        release elsewhere), a restart mid-press reusing the persistent
        handler, an exception in the button callback. Without this the drag
        captured on the DOWN fires DRAGGED every frame with nothing held, and
        the window / selection / column edge stays glued to the cursor.
        Synthesizing feed_up runs the normal release path (UP, DRAG_RELEASED,
        capture unlatched, cursor unpinned)."""
        probe = _BUTTON_PROBE.get("fn")
        if probe is None:
            return
        for input_id, state in list(self._states.items()):
            if not state.is_down:
                continue
            try:
                really_down = probe(input_id)
            except Exception:
                really_down = None
            if really_down is False:
                self.feed_up(input_id, self._cursor_x, self._cursor_y, t)

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
        self._view_cursor.clear()

    def register_hovered(self, view_id: Any, subscribed: list[str], priority: int = 0, tile_id=None, selected=False, blocker=False, cursor=None, cursor_rect=None):
        """Register hovered view. Priority 0 = topmost.

        cursor=<imgui MOUSE_CURSOR_*> names the pointer shape to show while
        this view is the topmost cursor-carrying hovered view (resolved in
        process_frame, same blocker/z rules as events). cursor_rect=(l, t, r, b)
        is the screen rect the shape covers: process_frame re-tests it against
        the LATEST pointer position, so a registration made at the start of a
        slow frame drops the moment the pointer has left (None = trust the
        hover test that registered it). An empty `subscribed` list is
        allowed: a cursor-only registration.

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
        if cursor is not None:
            self._view_cursor[view_id] = (cursor, cursor_rect)
        # Stamped even on a cursor-only (empty subscribed) registration: the
        # blocker pass keeps a blocker's OWN tile by this map.
        _view_id_to_tile_id[view_id] = tile_id

        # Parse new subscriptions
        new_subs = set()
        scroll_override = False
        for s in subscribed:
            if selected and s == "scroll_y_changed":
                priority -= 20
                # A *selected* view's scroll is the zoom-override gesture
                # (e.g. draw_texture): the -20 boost is meant to out-prioritize
                # its scroll parent so the wheel zooms instead of scrolling the
                # list. Mark it so the merge below preserves the boost.
                scroll_override = True
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
                merged_subs = subs | frozenset(new_subs)
                # A view_id collapses to ONE priority for all its subs, so by
                # default keep the WORST (max) - this stops a single boosted
                # subscription (e.g. a deeply-nested child's select) from
                # silently stealing the wheel from its scroll parent.
                #
                # EXCEPTION: the selected-scroll override. The render wrapper
                # registers that view's event params (incl. its -20 scroll boost)
                # under the bare tile_id, the SAME id the select gesture and the
                # right-click menu register on at baseline priority. With max()
                # that baseline wins the merge and erases the boost, so the
                # parent's view_scroll recaptures the wheel and scrolling the
                # selected child clears its own selection - the override never
                # fires. The boosted scroll registration is the child's last
                # tile_id registration, so taking the best (min) here preserves
                # the boost without affecting the non-selected case.
                if scroll_override:
                    merged_priority = min(pri, priority)
                else:
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

        if input_tap("down", input_id, x, y):
            return

        state = self._state(input_id)
        state.chord = False

        # MOUSE BUTTON CHORDS. The default resize is a right-drag; holding the
        # LEFT button too switches it to the top-left corner (core_render's
        # corner_drag_mode reads is_down("left_mouse") per frame). For that
        # handoff to be seamless the second button must be inert as an event
        # source - a left press landing mid-right-drag would otherwise start
        # a text selection / item pickup / window move under the cursor:
        #  - left pressed while right is held → the left press is a chord:
        #    level state only, nothing dispatched for it, its release silent.
        #  - right pressed while left is held but NOT yet dragging (both
        #    buttons pressed together, left arriving a few ms first) → the
        #    left press becomes the chord retroactively (its capture and any
        #    still-queued events are withdrawn) and the right press proceeds as
        #    a normal right-drag start with the left already down.
        #  - right pressed while a LEFT DRAG is already active (window corner,
        #    column edge, selection) → the right press is the chord: swallowed,
        #    so it can't trigger a second gesture on top of the first.
        if input_id == "left_mouse":
            other = self._states.get("right_mouse")
            if other is not None and other.is_down:
                state.is_down = True
                state.chord = True
                state.down_time = t
                state.down_x = x
                state.down_y = y
                state.is_double_press = False
                return
        elif input_id == "right_mouse":
            other = self._states.get("left_mouse")
            if other is not None and other.is_down and not other.chord:
                if self._drag_activated.get("left_mouse", False):
                    state.is_down = True
                    state.chord = True
                    state.down_time = t
                    state.down_x = x
                    state.down_y = y
                    state.is_double_press = False
                    return
                other.chord = True
                other.is_double_press = False
                self._drag_capture.pop("left_mouse", None)
                self._drag_activated.pop("left_mouse", None)
                self._down_origins.pop("left_mouse", None)
                self._pending = [e for e in self._pending
                                 if e.input_id != "left_mouse"]

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

        if input_tap("up", input_id, x, y):
            return

        state = self._state(input_id)
        was_down = state.is_down
        state.is_down = False

        if state.chord:
            # Releasing a chorded button: level state only - no UP, no click
            # (and no click_count / last_up_time bookkeeping, so it can't seed
            # a double-click either).
            state.chord = False
            return

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
        if input_tap("move", x, y):
            return
        self._cursor_x, self._cursor_y = x, y
        self._last_dx = dx
        self._last_dy = dy

        self._emit("cursor", EventAction.MOVED, x, y, dx, dy, t=t)

    def feed_change(self, input_id: str, value: float, t: float = None):
        if input_tap("change", input_id, value):
            return
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

    def feed_axes(self, input_id: str, axes, t: float = None):
        """A multi-axis CHANGED event — the 3D mouse's six axes in one
        InputEvent (`event.axes`), dispatched like any CHANGED input: to the
        topmost hovered view subscribed to "<input_id>_changed" (draw_voxels
        declares `space_mouse_changed=None`). Coalesced per frame by summing
        each component, the scroll rule: the reader feeds deflection × dt,
        so a slow frame that gathers several samples hands the view their
        integral, and nothing is dropped."""
        axes = tuple(float(a) for a in axes)
        if input_tap("axes", input_id, axes):
            return
        for e in self._pending:
            if e.input_id == input_id and e.action == EventAction.CHANGED:
                e.axes = tuple(a + b for a, b in zip(e.axes or (0.0,) * len(axes), axes))
                if t is not None:
                    e.timestamp = t
                return
        self._emit(input_id, EventAction.CHANGED, self._cursor_x, self._cursor_y, t=t)
        self._pending[-1].axes = axes

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

    def process_frame(self, on_pointer_down=None):
        """Returns {view_id: {event_name: event}} for all matched subscriptions.

        on_pointer_down observes each left/right press before dispatch, even
        without subscribers. It must not consume events or change registrations.
        """
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
        self._reconcile_held(t)

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
                # Window activation observes the press once, independently of
                # which control consumes it and captures the subsequent drag.
                if on_pointer_down is not None and event.input_id in ("left_mouse", "right_mouse"):
                    on_pointer_down(event)
                st = states.get(event.input_id)
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
                    # The shape showing at the press sticks through the drag.
                    self._drag_cursor[event.input_id] = self.cursor_shape

                # Record all currently hovered views as origin for this input
                down_origins[event.input_id] = {v for v, _, _ in self._hovered}

            # On UP, emit drag_released only if drag was activated. The release
            # variant mirrors the captured drag variant (double-drag → double).
            elif action == EventAction.UP:
                cap = drag_capture.pop(event.input_id, None)
                was_activated = drag_activated.pop(event.input_id, False)
                down_origins.pop(event.input_id, None)
                self._drag_cursor.pop(event.input_id, None)
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
                    # A double-click completed THIS frame.
                    pend = pending_clicks.pop(X, None)
                    if key_index.get((X, EventAction.DOUBLE_CLICKED)):
                        # A real double-click consumer (e.g. the voxel params
                        # panel on left double-click) handles it - drop the
                        # single click so it doesn't ALSO fire.
                        continue
                    # No double-click consumer (this input only has a double-DRAG
                    # gesture, e.g. right_mouse). A double-click with no drag is
                    # just one click - fire the ONE (the deferred first press)
                    # and drop this second CLICKED.
                    if pend is not None:
                        _, ev1, targets1 = pend
                        for v, k in targets1:
                            add_event(v, k, ev1)
                        continue
                    # else fall through: emit this lone click once.
                elif (key_index.get((X, EventAction.DOUBLE_CLICKED))
                        or key_index.get((X, EventAction.DOUBLE_DRAGGED))):
                    # Hold this click until the window expires. Cancelled by a
                    # double-DRAG activating (drag pass) or a double-click
                    # completing (above); otherwise it flushes as a single click.
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
                # A drag is not a click - drop any single click deferred for this
                # input (the 1st press of a double-drag) so the drag doesn't
                # also open the context panel when it ends.
                pending_clicks.pop(input_id, None)

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
            # Don't flush while the button is held again (a 2nd click in
            # progress) - wait for its release so a double-click/drag can't
            # claim it.
            for X in [k for k, v in pending_clicks.items()
                      if t >= v[0] and not (k in states and states[k].is_down)]:
                _, ev, targets = pending_clicks.pop(X)
                for v, k in targets:
                    add_event(v, k, ev)
            if pending_clicks:
                request_render()

        # --- Mouse-cursor shape (gl_gui/mouse_cursor.py) ---
        # A captured drag pins the shape that was showing at its press -
        # and mutes hover shapes afterwards (dragging a window across an
        # icon must not flash the I-beam). Otherwise the topmost hovered
        # view carrying a cursor wins; _hovered is priority-sorted and
        # already blocker-pruned, so a covered view never shows its shape.
        shape = None
        if drag_capture:
            for input_id in drag_capture:
                c = self._drag_cursor.get(input_id)
                if c is not None:
                    shape = c
                    break
        else:
            # Registrations are from the previous draw pass (hit-tested at
            # that frame's pointer). Re-check each shape's rect with the
            # pointer as it is NOW - the draw just ran - so the shape can
            # never outlive the pointer leaving its rect by a slow frame.
            # (A view the pointer has newly entered shows its shape once it
            # registers next frame; never sticking beats one frame of arrow.)
            vc = self._view_cursor
            pointer_x, pointer_y = self._cursor_x, self._cursor_y
            for v, _, _ in self._hovered:
                entry = vc.get(v)
                if entry is None:
                    continue
                c, rect = entry
                if rect is not None and not (rect[0] <= pointer_x <= rect[2]
                                             and rect[1] <= pointer_y <= rect[3]):
                    continue
                shape = c
                break
        self.cursor_shape = shape

        return result, result_by_type

    def is_down(self, input_id: str) -> bool:
        s = self._states.get(input_id)
        return s.is_down if s else False

    def cursor(self) -> tuple[float, float]:
        return (self._cursor_x, self._cursor_y)