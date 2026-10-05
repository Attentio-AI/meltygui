"""Native UIKit input feeding the existing cached-view InputHandler.

Call process_inputs(info, events) once before imgui.new_frame(). Melty then
calls pump() as usual before resolving subscriptions. UIKit points remain
logical coordinates; display_fb_scale alone carries the device pixel scale.

This is primary-touch and committed-text support. Gesture recognition, Pencil
predictions and UITextInput composition/selection are separate host work.
"""
from collections import deque
import math
import time

import meltygui_imgui as imgui

from meltygui.core.input.input_handler import input_tap, set_button_probe, parse_event_name
from meltygui.core.windowing import window_constants as codes


_KEY_NAMES = {value: name[4:].lower() for name, value in vars(codes).items()
              if name.startswith('KEY_') and name not in ('KEY_LAST', 'KEY_UNKNOWN')}
_KEY_NAMES.update({codes.KEY_LEFT: 'left_arrow', codes.KEY_RIGHT: 'right_arrow'})
_MODIFIER_KEYS = {codes.KEY_LEFT_SHIFT, codes.KEY_RIGHT_SHIFT, codes.KEY_LEFT_CONTROL,
                  codes.KEY_RIGHT_CONTROL, codes.KEY_LEFT_ALT, codes.KEY_RIGHT_ALT,
                  codes.KEY_LEFT_SUPER, codes.KEY_RIGHT_SUPER, codes.KEY_CAPS_LOCK,
                  codes.KEY_NUM_LOCK}
_IMGUI_KEYS = {'TAB': 'TAB', 'LEFT_ARROW': 'LEFT', 'RIGHT_ARROW': 'RIGHT',
               'UP_ARROW': 'UP', 'DOWN_ARROW': 'DOWN', 'PAGE_UP': 'PAGE_UP',
               'PAGE_DOWN': 'PAGE_DOWN', 'HOME': 'HOME', 'END': 'END', 'INSERT': 'INSERT',
               'DELETE': 'DELETE', 'BACKSPACE': 'BACKSPACE', 'SPACE': 'SPACE',
               'ENTER': 'ENTER', 'ESCAPE': 'ESCAPE', 'PAD_ENTER': 'KP_ENTER',
               **{key: key for key in ('A', 'C', 'V', 'X', 'Y', 'Z')}}


class IOSInput:
    def __init__(self, handler, backend, io, melty):
        self.handler, self.backend, self.io, self.melty = handler, backend, io, melty
        self.window = backend.window
        self._edit_pending = deque()
        self.edit_events = []
        self._last_presentation = None
        self._pressed_keys = set()
        self._touch_pressed = self._release_pending = False
        self._pointer_cancelled = True
        self._modifiers = 0
        self._keyboard_visible = None
        self._key_inputs = {}
        self._frame_cursor = self.window.cursor_pos
        self._body_action_hits = ()
        self._button_probe = self.button_really_down
        set_button_probe(self._button_probe)
        for imgui_name, native_name in _IMGUI_KEYS.items():
            io.key_map[getattr(imgui, 'KEY_' + imgui_name)] = getattr(codes, 'KEY_' + native_name)
        # UIKit owns the pointer; no desktop cursor shape calls for touch.
        io.config_flags |= imgui.CONFIG_NO_MOUSE_CURSOR_CHANGE
        io.get_clipboard_text_fn = lambda: backend.get_clipboard_string(self.window)
        io.set_clipboard_text_fn = lambda text: backend.set_clipboard_string(self.window, text)

    @property
    def has_pending_events(self):
        return bool(self._edit_pending or self._release_pending)

    @property
    def allow_hovering(self):
        return not imgui.is_window_hovered()

    def button_really_down(self, input_id):
        if input_id == 'left_mouse':
            return self.backend.get_mouse_button(self.window, codes.MOUSE_BUTTON_LEFT) == codes.PRESS
        return None

    def _set_modifiers(self, mods):
        self._modifiers = int(mods)
        self.io.key_shift = bool(mods & codes.MOD_SHIFT)
        self.io.key_ctrl = bool(mods & codes.MOD_CONTROL)
        self.io.key_alt = bool(mods & codes.MOD_ALT)
        self.io.key_super = bool(mods & codes.MOD_SUPER)
        self.handler.set_modifiers(shift=self.io.key_shift, ctrl=self.io.key_ctrl,
                                   alt=self.io.key_alt, meta=self.io.key_super)

    def _stamp(self, keyboard=False):
        now = time.monotonic()
        self.melty._last_input_time = self.melty._last_presence_time = now
        if keyboard:
            self.melty._last_key_time = now

    def _move(self, event):
        x, y = float(event['x']), float(event['y'])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('native touch coordinates must be finite')
        self.window.cursor_pos = x, y
        self.window.cursor_motion_generation += 1
        self.handler.feed_move(x, y, x - self._frame_cursor[0], y - self._frame_cursor[1], t=event['_time'])
        self.window.emit('cursor_pos', x, y)

    def _touch(self, event):
        window, kind = self.window, event['kind']
        identity = event['touch_id']
        if kind == 'touch_begin':
            if identity in window.touches:
                return  # a repeated begin must not restart the current drag
            if not window.touches:
                window.primary_touch = identity
            window.touches[identity] = event
            if window.primary_touch != identity:
                return
            self._frame_cursor = float(event['x']), float(event['y'])
            self._pointer_cancelled = False
            self._move(event)
            window.buttons[codes.MOUSE_BUTTON_LEFT] = codes.PRESS
            window.hovered = True
            self.melty._pointer_inside = True
            self._touch_pressed = True
            self.handler.feed_down('left_mouse', *window.cursor_pos, t=event['_time'])
            window.emit('cursor_enter', True)
            window.emit('mouse_button', codes.MOUSE_BUTTON_LEFT, codes.PRESS, self._modifiers)
        elif identity not in window.touches:
            return  # UIKit cancellation/overflow may retire this identity first
        elif kind == 'touch_move':
            window.touches[identity] = event
            if identity != window.primary_touch:
                return
            self._move(event)
        else:
            del window.touches[identity]
            if identity != window.primary_touch:
                return
            if kind == 'touch_end':
                self._move(event)
                self.handler.feed_up('left_mouse', *window.cursor_pos, t=event['_time'])
                window.emit('mouse_button', codes.MOUSE_BUTTON_LEFT, codes.RELEASE, self._modifiers)
            else:
                self.handler.feed_cancel('left_mouse')
                self._touch_pressed = False
                self._pointer_cancelled = True
            window.buttons[codes.MOUSE_BUTTON_LEFT] = codes.RELEASE
            window.primary_touch = None
            window.hovered = False
            self.melty._pointer_inside = False
            window.emit('cursor_enter', False)
            # Other fingers never inherit a completed primary gesture. A new
            # primary is chosen only after every contact has ended/cancelled.
        self._stamp()

    def _key(self, key, action, mods, timestamp):
        key, mods = int(key), int(mods)
        if not 0 <= key < len(self.io.keys_down):
            raise ValueError(f'native key code {key} is outside the ImGui key range')
        if action not in (codes.PRESS, codes.REPEAT, codes.RELEASE):
            raise ValueError(f'unsupported native key action {action}')
        self._set_modifiers(mods)
        name = _KEY_NAMES.get(key, f'key_{key}')
        if action in (codes.PRESS, codes.REPEAT):
            if input_tap('key', key, mods):
                return
            self.window.keys[key] = codes.PRESS
            self._pressed_keys.add(key)
            self.melty.frame_key_events.append((key, mods))
            self.edit_events.append(('key', (key, mods)))
            if action == codes.PRESS:
                self._key_inputs[key] = name
                if key not in _MODIFIER_KEYS:
                    self.melty._keys_down.add(key)
                self.handler.feed_down(name, *self.window.cursor_pos, t=timestamp)
        else:
            self.window.keys[key] = codes.RELEASE
            self.melty._keys_down.discard(key)
            self._key_inputs.pop(key, None)
            self.handler.feed_up(name, *self.window.cursor_pos, t=timestamp)
        self.window.emit('key', key, 0, action, mods)
        self._stamp(keyboard=True)

    def _text(self, text):
        # Keep the same recording/replay tap as desktop character callbacks.
        text = ''.join(char for char in text if not input_tap('char', ord(char)))
        if not text:
            return
        self.melty.frame_text_events.append(text)
        self.edit_events.append(('text', text))
        self.io.add_input_characters_utf8(text)
        for char in text:
            self.window.emit('char', ord(char))
        self._stamp(keyboard=True)

    def _drain_edit_batch(self):
        if not self._edit_pending:
            return
        event = self._edit_pending.popleft()
        if event['kind'] == 'text' and event['text'] not in ('\n', '\r', '\t'):
            text = event['text']
            while (self._edit_pending and self._edit_pending[0]['kind'] == 'text'
                   and self._edit_pending[0]['text'] not in ('\n', '\r', '\t')):
                text += self._edit_pending.popleft()['text']
            self._text(text)
        elif event['kind'] == 'key':
            self._key(event['key'], event['action'], event.get('modifiers', 0), event['_time'])
        else:
            key = (codes.KEY_BACKSPACE if event['kind'] == 'backspace' else
                   codes.KEY_TAB if event['text'] == '\t' else codes.KEY_ENTER)
            self._key(key, codes.PRESS, event.get('modifiers', 0), event['_time'])
            self._key(key, codes.RELEASE, event.get('modifiers', 0), event['_time'])

    def process_inputs(self, info, events):
        """Consume fresh pointer events and one ordered editor batch per frame.

        The current editor processes text and navigation in separate phases.
        Splitting text/control batches preserves their order without changing
        those phases. Pending edits request another native display-link frame.
        """
        self.backend.update_frame(info)
        self.melty.frame_key_events = []
        self.melty.frame_text_events = []
        self.edit_events = []
        self._pressed_keys.clear()
        self._touch_pressed = self._release_pending = False
        self._frame_cursor = self.window.cursor_pos
        self.io.mouse_wheel = self.io.mouse_wheel_horizontal = 0.0
        now = time.perf_counter()
        offset = now - float(info.get('now', now))
        presentation = float(info.get('presentation_time', info.get('now', now)))
        delta = presentation - self._last_presentation if self._last_presentation is not None else 1 / 60
        self.io.delta_time = delta if math.isfinite(delta) and delta > 0 else 1 / 60
        self._last_presentation = presentation
        for raw in events:
            event = dict(raw)
            event['_time'] = float(event.get('timestamp', info.get('now', now))) + offset
            if not math.isfinite(event['_time']):
                raise ValueError('native input timestamps must be finite')
            kind = event['kind']
            if kind in ('touch_begin', 'touch_move', 'touch_end', 'touch_cancel'):
                self._touch(event)
            elif kind == 'cancel_all':
                self._cancel_inputs()
            elif kind in ('text', 'key', 'backspace'):
                self._edit_pending.append(event)
            elif kind == 'scroll':
                for axis in ('x', 'y'):
                    value = float(event.get(axis, 0))
                    if value:
                        self.handler.feed_change('scroll_' + axis, value, t=event['_time'])
                self.io.mouse_wheel_horizontal += float(event.get('x', 0))
                self.io.mouse_wheel += float(event.get('y', 0))
                self.window.emit('scroll', float(event.get('x', 0)), float(event.get('y', 0)))
                self._stamp()
            else:
                raise ValueError(f'unsupported native iOS input event {kind!r}')
        self._drain_edit_batch()
        self.io.display_size = self.backend.get_window_size(self.window)
        self.io.display_fb_scale = self.backend.get_window_content_scale(self.window)
        self.io.mouse_pos = (-1e30, -1e30) if self._pointer_cancelled else self.window.cursor_pos
        self.io.mouse_down[0] = bool(self.window.buttons.get(0, 0) or self._touch_pressed)
        for button in range(1, len(self.io.mouse_down)):
            self.io.mouse_down[button] = False
        for key in range(len(self.io.keys_down)):
            self.io.keys_down[key] = bool(self.window.keys.get(key, 0) or key in self._pressed_keys)
        # ImGui 1.82 has level-only IO. A tap entirely between frames still
        # needs a visible down frame, followed by a release frame. InputHandler
        # receives both native edges immediately, without this IO-only latch.
        self._release_pending = bool(
            (self._touch_pressed and not self.window.buttons.get(0)) or
            any(not self.window.keys.get(key) for key in self._pressed_keys))
        if self.has_pending_events:
            self.backend.post_empty_event()

    def pump(self):
        """Refresh held-input recency; queued native edges were already fed."""
        self._set_modifiers(self._modifiers)
        self._refresh_touch_targets()
        if self.window.primary_touch is not None or self.melty._keys_down:
            self._stamp()

    def _refresh_touch_targets(self):
        """Re-hit cached body actions before dispatching a new native touch.

        UIKit has no hover frame before a finger lands. The previous render's
        subscriptions therefore describe the previous contact, even though
        begin_frame has already hit-tested this contact against the live BVH.
        Named body actions and declared parameters can be re-hit separately;
        retain the remaining merged window-chrome subscriptions and captures.
        """
        hits = tuple(self.melty.bvh_query(*self.io.mouse_pos))
        previous, self._body_action_hits = self._body_action_hits, hits
        if not self._touch_pressed:
            return
        # The BVH can retain old tab/conditional-child geometry. Only revive
        # controls whose pixels survived the last render (including a cached
        # ancestor's snapshot); a replaced branch must not steal the tap.
        hits = tuple(view for view in hits if self.melty.cache._pixels_preserved(view))
        views, descendants, current_views = {}, set(), set()
        for current, roots in ((True, hits), (False, previous)):
            pending = list(roots)
            while pending:
                view = pending.pop()
                if current:
                    current_views.add(id(view))
                if id(view) in views:
                    continue
                views[id(view)] = view
                for child in (*view._children.values(), *view._view_children.values()):
                    if (child is not None and child is not view
                            and getattr(child._wrapper, 'fast_host', False)):
                        if current:
                            descendants.add(id(child))
                        pending.append(child)
        stale, parameters = set(), {}
        for view in views.values():
            names = getattr(view._wrapper, '__params__', ())
            parameters[view._tile_id] = {
                parse_event_name(name)[:2] for name in names if name in view._kwargs}
            record = view._body_actions
            if record:
                stale.update(f'{view._tile_id}_{action[0]}' for action in record[1]
                             if action[0] is not None)
        handler = self.handler
        handler._hovered[:] = [
            (identity, priority, subscriptions - parameters.get(identity, set()))
            for identity, priority, subscriptions in handler._hovered if identity not in stale]
        for identity in stale:
            handler._view_cursor.pop(identity, None)
        # Retain queued edges, previous hover, blockers and drag captures. A
        # begin_frame() here would discard the touch we are about to deliver.
        roots = [view for view in hits if id(view) not in descendants]
        if not roots:
            return
        # _register_action selects the debug overlay channel even with debug
        # drawing off. Normal begin_frame splits it after input dispatch, so
        # give this input-only replay a temporary split and merge it back.
        overlay = imgui.get_overlay_draw_list()
        overlay.channels_split(self.melty.max_layer)
        try:
            for view in roots:
                view.replay_body_actions()
            for identity in current_views:
                view = views[identity]
                names = getattr(view._wrapper, '__params__', ())
                if names:
                    view.register_parameter_actions(names, view._kwargs, view._event_rects, view.closable)
        finally:
            overlay.channels_merge()

    def update_keyboard(self, visible=None):
        """Call after drawing, when the app has resolved its text-focus owner."""
        if visible is None:
            visible = bool(self.melty.text_focused_ds is not None or self.io.want_text_input)
        if bool(visible) != self._keyboard_visible:
            self.backend.set_keyboard_visible(visible)
            self._keyboard_visible = bool(visible)

    def _cancel_inputs(self):
        # Committed text is retained across suspension. Hardware edges from
        # before the cancellation must not resurrect a held key after resume.
        self._edit_pending = deque(event for event in self._edit_pending if event['kind'] != 'key')
        self.handler.feed_cancel('left_mouse')
        for name in self._key_inputs.values():
            self.handler.feed_cancel(name)
        self._key_inputs.clear()
        self.window.touches.clear()
        self.window.primary_touch = None
        self.window.keys.clear()
        self.window.buttons.clear()
        self.window.hovered = False
        self.melty._pointer_inside = False
        self.melty._keys_down.clear()
        self._pressed_keys.clear()
        self._touch_pressed = self._release_pending = False
        self._pointer_cancelled = True
        self._set_modifiers(0)
        for button in range(len(self.io.mouse_down)):
            self.io.mouse_down[button] = False
        for key in range(len(self.io.keys_down)):
            self.io.keys_down[key] = False

    def suspend(self):
        self._cancel_inputs()
        self.backend.set_active(False)
        self._last_presentation = None
        self.update_keyboard(False)

    def resume(self):
        self.backend.set_active(True)
        self._last_presentation = None
        self.backend.post_empty_event()

    def close(self):
        self.suspend()
        self._edit_pending.clear()
        set_button_probe(None)
