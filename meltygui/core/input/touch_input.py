"""Primary-contact gestures in logical pixels, shared by touch backends.

InputHandler owns this state. Targets are resolved once at contact start using
the same blocker/priority index as mouse input, and remain captured off-view.
"""
import math

from meltygui.core.input.input_handler import _InputState


# Tune in logical pixels / seconds, independently of framebuffer density.
TOUCH_SLOP = 8.0
DECELERATION_RATE = 0.998  # UIScrollView.DecelerationRate.normal
FLICK_DECAY = -1000.0 * math.log(DECELERATION_RATE)
FLICK_PROJECTION = DECELERATION_RATE / (1000.0 * (1.0 - DECELERATION_RATE))
MIN_FLING_SPEED = 100.0
STOP_DISTANCE = 0.25
REPEAT_FLICK_WINDOW = 0.35
REPEAT_FLICK_MIN = 800.0
FLICK_PAUSE = 0.10


def flick_velocity(speed):
    amount = max(0.0, min(1.0, (abs(speed) - 1200.0) / 2800.0))
    return speed * (1.0 + 1.25 * amount * amount * (3.0 - 2.0 * amount))


def scroll_axes(dx, dy, horizontal, vertical):
    if horizontal and vertical:
        if abs(dx) >= 12.0 and abs(dx) >= 2.0 * abs(dy):
            return True, False
        if abs(dy) > TOUCH_SLOP:
            return False, True
    elif horizontal and abs(dx) > TOUCH_SLOP:
        return True, False
    elif vertical and abs(dy) > TOUCH_SLOP:
        return False, True
    return None


class TouchInput(_InputState):
    def __init__(self):
        super().__init__()
        self.events = []
        self.active = False
        self.mouse = False
        self.moved = False
        self.origin = self.position = (0.0, 0.0)
        self.scroll_target = self.tap_target = None
        self.scroll_x_target = None
        self.press_observers = ()
        self.velocity = 0.0
        self.velocity_x = 0.0
        self.last_motion = self.coast_time = 0.0
        self.contact_time = 0.0
        self.axes = None
        self.stopping = self.stop_contact = False
        self.carry = None
        self.estimated_velocity = (0.0, 0.0)

    def remaining_velocity(self, timestamp):
        decay = math.exp(-FLICK_DECAY * max(0.0, timestamp - self.coast_time))
        return tuple(v * decay if abs(v * decay * FLICK_PROJECTION) > STOP_DISTANCE else 0.0
                     for v in (self.velocity_x, self.velocity))

    def has_momentum(self, timestamp):
        return not self.active and any(self.remaining_velocity(timestamp))

    def cancel(self, handler):
        self.events.clear()
        self.active = self.mouse = False
        self.stopping = self.stop_contact = False
        self.carry = self.axes = None
        self.estimated_velocity = (0.0, 0.0)
        self.velocity = 0.0
        self.velocity_x = 0.0
        self.scroll_target = self.tap_target = None
        self.scroll_x_target = None
        handler.feed_cancel('left_mouse')

    def dispatch(self, handler, index, deliver, now, on_pointer_down=None):
        from meltygui.core.input.input_handler import InputEvent, _view_id_flags_cache

        scroll_key = ('touch_scroll', 'changed')
        scroll_x_key = ('touch_scroll_x', 'changed')
        tap_key = ('touch', 'clicked')
        down_key = ('left_mouse', 'down')
        totals = {}
        if any((event.action == 'down' and event.input_id in ('left_mouse', 'right_mouse', 'middle_mouse'))
               or event.input_id in ('scroll_x', 'scroll_y') for event in handler._pending):
            self.velocity = self.velocity_x = 0.0

        def non_blocking(view, key):
            return _view_id_flags_cache.get(view, {}).get(key, (False, False))[1]

        def scroll(delta_x, delta_y, timestamp):
            for target, key, delta in ((self.scroll_x_target, scroll_x_key, delta_x),
                                       (self.scroll_target, scroll_key, delta_y)):
                if target is not None and delta:
                    previous = totals.get((target, key))
                    totals[target, key] = InputEvent(key[0], 'changed',
                        x=self.position[0], y=self.position[1],
                        value=delta + (previous.value if previous else 0.0), timestamp=timestamp)

        events, self.events = self.events, []
        for kind, x, y, timestamp, release_velocity in events:
            if kind == 'begin':
                remaining = self.remaining_velocity(timestamp) if not self.active else (0.0, 0.0)
                self.stopping = any(remaining)
                self.carry = ((self.scroll_x_target, self.scroll_target), remaining, timestamp) if self.stopping else None
                self.axes = None
                self.estimated_velocity = (0.0, 0.0)
                self.contact_time = timestamp
                self.velocity = 0.0
                self.velocity_x = 0.0
                self.active, self.moved = True, False
                self.origin = self.position = (x, y)
                self.last_motion = timestamp
                taps = handler._resolve_subscribers(tap_key, index)
                self.press_observers = tuple(v for v in handler._resolve_subscribers(down_key, index)
                                             if non_blocking(v, down_key))
                drags = handler._resolve_subscribers(('left_mouse', 'dragged'), index)
                scrolls = handler._resolve_subscribers(scroll_key, index)
                scrolls_x = handler._resolve_subscribers(scroll_x_key, index)
                # Text offers a touch tap instead of mouse selection. Explicit
                # handles/widgets still own their normal mouse drag on touch.
                self.tap_target = taps[0] if taps else None
                if self.tap_target is not None:
                    tap_priority = next(p for v, p in index[tap_key] if v == self.tap_target)
                    controls = [v for key in (('left_mouse', 'down'), ('left_mouse', 'clicked'))
                                for v, p in index.get(key, ())
                                if p < tap_priority and not non_blocking(v, key)]
                    if controls:
                        self.tap_target = None
                self.mouse = bool(drags and drags[0] not in taps)
                self.scroll_target = scrolls[0] if scrolls and not self.mouse else None
                self.scroll_x_target = scrolls_x[0] if scrolls_x and not self.mouse else None
                self.stop_contact = self.stopping and (self.mouse or not (scrolls or scrolls_x))
                if self.stop_contact:
                    continue
                if self.mouse or (self.tap_target is None and self.scroll_target is None
                                  and self.scroll_x_target is None):
                    self.mouse = True
                    handler.feed_down('left_mouse', x, y, t=timestamp)
            elif not self.active:
                continue
            else:
                old_x, old_y = self.position
                self.position = (x, y)
                dx, dy = x - self.origin[0], y - self.origin[1]
                self.moved |= dx * dx + dy * dy > TOUCH_SLOP * TOUCH_SLOP
                if self.stop_contact:
                    if kind == 'end':
                        self.active = False
                    continue
                if self.moved and not self.mouse:
                    previous_axes = self.axes
                    if self.axes is None:
                        self.axes = scroll_axes(dx, dy, self.scroll_x_target is not None,
                                                self.scroll_target is not None)
                    if self.axes is not None:
                        # Include sub-slop travel once, then retain the chosen axis.
                        scroll((x - old_x if previous_axes else dx) if self.axes[0] else 0.0,
                               (y - old_y if previous_axes else dy) if self.axes[1] else 0.0, timestamp)
                    # Backends without native velocity may estimate from moves,
                    # never from the noisy, often tiny final lift-off interval.
                    elapsed = timestamp - self.last_motion
                    if kind == 'move' and (x != old_x or y != old_y):
                        self.estimated_velocity = (((x - old_x) / elapsed, (y - old_y) / elapsed)
                                                   if 0 < elapsed <= FLICK_PAUSE else (0.0, 0.0))
                        self.last_motion = timestamp
                if kind == 'end':
                    if self.mouse:
                        handler.feed_up('left_mouse', x, y, t=timestamp)
                    elif not self.moved and not self.stopping:
                        if self.tap_target is not None:
                            # An immediate tap: mouse double-click deferral
                            # must not delay the caret or software keyboard.
                            event = InputEvent('touch', 'clicked', x=x, y=y,
                                               timestamp=timestamp)
                            deliver(self.tap_target, tap_key, event)
                            press = InputEvent('left_mouse', 'down', x=x, y=y, timestamp=timestamp)
                            for observer in self.press_observers:
                                deliver(observer, down_key, press)
                            if on_pointer_down is not None:
                                on_pointer_down(press)
                        else:
                            handler.feed_down('left_mouse', x, y, t=timestamp)
                            handler.feed_up('left_mouse', x, y, t=timestamp)
                    measured = release_velocity
                    if measured is None:
                        measured = self.estimated_velocity if timestamp - self.last_motion <= FLICK_PAUSE else (0.0, 0.0)
                    axes = self.axes or (False, False)
                    measured = tuple(v if enabled and math.isfinite(v) else 0.0
                                     for v, enabled in zip(measured, axes))
                    if math.hypot(*measured) >= MIN_FLING_SPEED:
                        velocity = [flick_velocity(v) for v in measured]
                        if self.carry is not None:
                            targets, carry, interrupted = self.carry
                            held = timestamp - self.contact_time
                            if targets == (self.scroll_x_target, self.scroll_target) and 0 <= held <= REPEAT_FLICK_WINDOW:
                                decay = math.exp(-FLICK_DECAY * max(held, timestamp - interrupted, 0.0))
                                for axis, speed in enumerate(measured):
                                    if abs(speed) >= REPEAT_FLICK_MIN and speed * carry[axis] > 0:
                                        velocity[axis] += carry[axis] * decay
                        self.velocity_x, self.velocity = velocity
                    self.carry = None
                    self.active = False
                    self.coast_time = timestamp

        if not self.active and (self.velocity or self.velocity_x):
            remaining_x, remaining_y = self.remaining_velocity(now)
            scroll((self.velocity_x - remaining_x) * FLICK_PROJECTION,
                   (self.velocity - remaining_y) * FLICK_PROJECTION, now)
            self.velocity_x, self.velocity = remaining_x, remaining_y
            self.coast_time = now
        for (target, key), event in totals.items():
            deliver(target, key, event)
        return not self.active and bool((self.scroll_target is not None and self.velocity)
                                       or (self.scroll_x_target is not None and self.velocity_x))
