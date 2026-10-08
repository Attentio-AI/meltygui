"""Primary-contact gestures in logical pixels, shared by touch backends.

InputHandler owns this state. Targets are resolved once at contact start using
the same blocker/priority index as mouse input, and remain captured off-view.
"""
import math

from meltygui.core.input.input_handler import _InputState


# Tune in logical pixels / seconds, independently of framebuffer density.
TOUCH_SLOP = 8.0
FLICK_DECAY = 8.0
FLICK_MIN_SPEED = 30.0
FLICK_MAX_SPEED = 4000.0
FLICK_PAUSE = 0.10


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

    def cancel(self, handler):
        self.events.clear()
        self.active = self.mouse = False
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
        for kind, x, y, timestamp in events:
            if kind == 'begin':
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
                was_moved = self.moved
                self.moved |= dx * dx + dy * dy > TOUCH_SLOP * TOUCH_SLOP
                if self.moved and not self.mouse:
                    # Include travel accumulated below slop, then use each
                    # sample exactly once (including the final release sample).
                    scroll(x - old_x if was_moved else dx, y - old_y if was_moved else dy, timestamp)
                    elapsed = timestamp - self.last_motion
                    if x != old_x or y != old_y:
                        if 0 < elapsed <= FLICK_PAUSE:
                            speed = (y - old_y) / elapsed
                            self.velocity = max(-FLICK_MAX_SPEED, min(FLICK_MAX_SPEED, speed))
                            speed_x = (x - old_x) / elapsed
                            self.velocity_x = max(-FLICK_MAX_SPEED, min(FLICK_MAX_SPEED, speed_x))
                        else:
                            self.velocity = 0.0
                            self.velocity_x = 0.0
                        self.last_motion = timestamp
                if kind == 'end':
                    if self.mouse:
                        handler.feed_up('left_mouse', x, y, t=timestamp)
                    elif not self.moved:
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
                    if timestamp - self.last_motion > FLICK_PAUSE:
                        self.velocity = 0.0
                        self.velocity_x = 0.0
                    self.active = False
                    self.coast_time = now

        coasting = ((self.scroll_target is not None and self.velocity)
                    or (self.scroll_x_target is not None and self.velocity_x))
        if not self.active and coasting:
            elapsed = max(0.0, now - self.coast_time)
            decay = math.exp(-FLICK_DECAY * elapsed)
            scroll(self.velocity_x * (1.0 - decay) / FLICK_DECAY,
                   self.velocity * (1.0 - decay) / FLICK_DECAY, now)
            self.velocity *= decay
            self.velocity_x *= decay
            self.coast_time = now
            if abs(self.velocity) < FLICK_MIN_SPEED:
                self.velocity = 0.0
            if abs(self.velocity_x) < FLICK_MIN_SPEED:
                self.velocity_x = 0.0
        for (target, key), event in totals.items():
            deliver(target, key, event)
        return not self.active and bool((self.scroll_target is not None and self.velocity)
                                       or (self.scroll_x_target is not None and self.velocity_x))
