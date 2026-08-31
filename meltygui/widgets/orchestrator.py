"""Orchestrator: record and replay user input (mouse, keys, scroll, text),
managed as a collection of named orchestrations (AppModel.orchestrations).

Recording taps the ONE funnel all real input flows through —
InputHandler.feed_down/up/move/change (via input_handler.set_input_tap) plus
the key/char callbacks in event_backends.py — so a take is the same stream
the app saw. Replay re-feeds that stream into the same handler
(Orchestrator.pump, driven from Melty right before process_frame) and stamps
a VIRTUAL cursor / buttons / modifiers over imgui's io
(Orchestrator.stamp_io, from SplitOverlayRenderer.process_inputs), so
nothing touches the real pointer — no Wayland warping, and real input is
muted at the funnel while a replay drives (Esc aborts).

Replay is not blind: while recording, every new GROUP on the undo stacks
(UndoManager edits + NavUndo window/location steps) is stamped as a CUE at
the current event index. Replay pauses at each cue until the live stack
shows a matching change; a missing cue first re-aims the last click at the
cue target's LIVE rect (the recorded window may have moved), then aborts
with a notice. With an orchestration's restore checkbox on, finishing a
replay walks both undo stacks back to where they stood at replay start —
the undo stack IS the restore mechanism.

The window is fast_dock-style: raw draw-list rows, manual hit-testing,
clicks resolved from left_mouse_down while hovered; orchestrator_sync()
(called once per frame from the always-rendering root) polls cue capture
and repaints the cached tile when engine state changes.
"""
import time

import glfw
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.events.input_handler import set_input_tap
from src.lsd.gl_gui.notifications import notify
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.core_undo import UndoManager, NavUndo
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

# input_id -> imgui io.mouse_down index (the buttons stamp_io overrides).
_IMGUI_BUTTON = {"left_mouse": 0, "right_mouse": 1, "middle_mouse": 2}

# Feed ids / key codes of the stop hotkey (Ctrl+Shift+O), trimmed off a
# take's tail when the hotkey stops the recording.
_HOTKEY_KEYS = (glfw.KEY_O, glfw.KEY_LEFT_CONTROL, glfw.KEY_RIGHT_CONTROL,
                glfw.KEY_LEFT_SHIFT, glfw.KEY_RIGHT_SHIFT)
_HOTKEY_FEED_IDS = {f"key_{key}" for key in _HOTKEY_KEYS}


def _stacks():
    """The undo timelines cues are cut from and restore unwinds — name ->
    UndoStack. Restore runs in this dict's ORDER: edits first (their undo
    requests need the views still open), navigation after."""
    return {"edits": UndoManager.stack, "navigation": NavUndo.stack}


def _short_repr(value):
    try:
        return repr(value)[:120]
    except Exception:
        return "?"


class Orchestrator:
    """The record/replay engine. All state in class attributes (hotswap keeps
    live values, same as UndoManager). Exactly one of `recording` /
    `replaying` is non-None at a time; `_restore_steps` runs after a replay
    finishes with restore checked."""

    recording = None            # Orchestration being recorded into
    replaying = None            # Orchestration being replayed
    status = ""                 # one-line state for the window's footer

    _record_t0 = 0.0
    _record_marks = {}          # stack name -> group id at recording start / last cue stamp

    _replay_t0 = 0.0
    _replay_index = 0           # next event to inject
    _replay_marks = {}          # stack name -> group id at replay start (restore + restore baseline)
    _cue_cursor = 0             # next cue (index into orch.cues) not yet armed
    _cue_pending = None         # armed cue awaiting verification
    _cue_wait = 0
    _cue_corrected = False
    _matched_ids = set()        # id(change) of live changes already claimed by a cue
    _last_click = None          # (input_id, x, y) of the last injected press
    _injecting = False          # True while pump feeds the handler (tap lets those through)
    _restore_steps = []         # [(stack_name, mark_gid), ...] still to unwind

    # Virtual input state stamped over imgui's io while replaying.
    _virtual_x = 0.0
    _virtual_y = 0.0
    _virtual_buttons = {}       # input_id -> True while virtually depressed
    _virtual_mods = 0           # glfw mod state from the last injected key
    _virtual_wheel = 0.0        # accumulated scroll for io.mouse_wheel this frame
    _virtual_chars = []         # codepoints queued for io.add_input_char

    # Stamped by the window body each frame: (left, top, right, bottom) rect
    # used to trim the stop click off a take's tail.
    window_rect = None

    # ── the funnel ───────────────────────────────────────────────────────

    @classmethod
    def tap(cls, kind, *args):
        """input_handler's input_tap target: sees every REAL input event.
        Returns True to consume it (replay/restore mute)."""
        if cls._injecting:
            return False                      # our own injection - let it through
        if cls.replaying is not None or cls._restore_steps:
            if kind == "key" and args and args[0] == glfw.KEY_ESCAPE:
                cls.abort("Esc")
            return True                       # mute real input while driving
        take = cls.recording
        if take is not None:
            now = round(time.monotonic() - cls._record_t0, 4)
            if kind == "move" and take.events:
                last = take.events[-1]
                if last[1] == "move" and (
                        abs(args[0] - last[2]) < Toggles.Orchestrator.move_sample_min_px
                        and abs(args[1] - last[3]) < Toggles.Orchestrator.move_sample_min_px):
                    return False              # sub-pixel jitter - skip
            take.events.append((now, kind) + tuple(args))
        return False

    # ── recording ────────────────────────────────────────────────────────

    @classmethod
    def start_recording(cls, orchestration):
        if cls.replaying is not None or cls._restore_steps:
            return
        orchestration.events = []
        orchestration.cues = []
        orchestration.duration = 0.0
        cls.recording = orchestration
        cls._record_t0 = time.monotonic()
        cls._record_marks = {name: stack._next_group_id
                             for name, stack in _stacks().items()}
        cls.status = "recording"
        request_render()

    @classmethod
    def stop_recording(cls, via_hotkey=False):
        take = cls.recording
        cls.recording = None
        cls.status = ""
        if take is None:
            return
        cls.poll_recording_into(take)         # cut cues one last time
        if via_hotkey:
            cls._trim_hotkey_tail(take)
        else:
            cls._trim_window_tail(take)
        # Leading "up" events are the release of the click that pressed
        # Record - they belong to the button gesture, not the take.
        while take.events and take.events[0][1] == "up":
            take.events.pop(0)
        take.duration = round(take.events[-1][0], 2) if take.events else 0.0
        request_render()

    @classmethod
    def poll_recording(cls):
        """Once per frame from orchestrator_sync: cut a CUE for every new
        undo GROUP that appeared since the last sweep."""
        if cls.recording is not None:
            cls.poll_recording_into(cls.recording)

    @classmethod
    def poll_recording_into(cls, take):
        for name, stack in _stacks().items():
            mark = cls._record_marks.get(name, 0)
            new_groups = {}
            for change in stack.history:
                if change.group_id > mark:
                    # first change of each group anchors the cue
                    new_groups.setdefault(change.group_id, change)
            for gid in sorted(new_groups):
                change = new_groups[gid]
                kind = type(change).__name__
                if kind == "CaretChange":
                    continue                  # caret changes replay implicitly - pure noise as cues
                tile = getattr(change.draw_state, "_tile_id", None)
                take.cues.append((len(take.events), name, kind,
                                  str(change.display_name),
                                  _short_repr(change.new), repr(tile)[:200]))
                mark = max(mark, gid)
            cls._record_marks[name] = max(cls._record_marks.get(name, 0), mark)

    @classmethod
    def _trim_window_tail(cls, take):
        """Drop the trailing events of the click that pressed Stop — the last
        press inside the Orchestrator window's rect, and everything after."""
        rect = cls.window_rect
        if rect is None:
            return
        left, top, right, bottom = rect
        for index in range(len(take.events) - 1, -1, -1):
            event = take.events[index]
            if event[1] == "down" and len(event) >= 5 \
                    and left <= event[3] <= right and top <= event[4] <= bottom:
                del take.events[index:]
                return

    @classmethod
    def _trim_hotkey_tail(cls, take):
        """Drop the trailing Ctrl+Shift+O keystrokes the stop hotkey put on
        the tail (its key events were recorded before the hotkey fired)."""
        while take.events:
            event = take.events[-1]
            if event[1] == "key" and event[2] in _HOTKEY_KEYS:
                take.events.pop()
            elif event[1] in ("down", "up") and event[2] in _HOTKEY_FEED_IDS:
                take.events.pop()
            else:
                return

    # ── replay ─────────────────────────────────────────────────────────────

    @classmethod
    def play(cls, orchestration):
        if cls.recording is not None or cls.replaying is not None or cls._restore_steps:
            return
        if not orchestration.events:
            notify("Orchestration is empty — record it first", tag="orchestrator")
            return
        cls.replaying = orchestration
        cls._replay_t0 = time.monotonic()
        cls._replay_index = 0
        cls._cue_cursor = 0
        cls._cue_pending = None
        cls._cue_wait = 0
        cls._cue_corrected = False
        cls._matched_ids = set()
        cls._last_click = None
        cls._replay_marks = {name: stack._next_group_id
                             for name, stack in _stacks().items()}
        handler = Melty.event_handler
        cls._virtual_x, cls._virtual_y = handler.cursor()
        cls._virtual_buttons = {}
        cls._virtual_mods = 0
        cls._virtual_wheel = 0.0
        cls._virtual_chars = []
        cls.status = f"replaying 0/{len(orchestration.events)}"
        request_render()

    @classmethod
    def pump(cls):
        """Per frame from Melty, after the real backend's pump and before
        process_frame: inject every event whose recorded time has elapsed,
        pausing at cues; step a pending restore one undo group per frame
        (undo writes apply on the NEXT frame's render, so batching them in
        one frame would overwrite each other)."""
        if cls._restore_steps:
            cls._step_restore()
            request_render()
            return
        orchestration = cls.replaying
        if orchestration is None:
            return
        request_render()
        if cls._cue_pending is not None and not cls._resolve_pending_cue():
            return
        speed = max(0.05, Toggles.Orchestrator.replay_speed)
        now = (time.monotonic() - cls._replay_t0) * speed
        events = orchestration.events
        cues = orchestration.cues
        while cls._replay_index < len(events):
            if cls._cue_cursor < len(cues) \
                    and cues[cls._cue_cursor][0] <= cls._replay_index:
                cls._cue_pending = cues[cls._cue_cursor]
                cls._cue_cursor += 1
                cls._cue_wait = 0
                cls._cue_corrected = False
                if not cls._resolve_pending_cue():
                    return
                continue
            event = events[cls._replay_index]
            if event[0] > now:
                break
            cls._inject(event)
            cls._replay_index += 1
            cls.status = f"replaying {cls._replay_index}/{len(events)}"
        if cls._replay_index >= len(events):
            # Trailing cues (the final click's undo change lands AFTER the
            # last event) still gate the finish - arm and resolve them here.
            while cls._cue_pending is None and cls._cue_cursor < len(cues):
                cls._cue_pending = cues[cls._cue_cursor]
                cls._cue_cursor += 1
                cls._cue_wait = 0
                cls._cue_corrected = False
                if not cls._resolve_pending_cue():
                    return
            if cls._cue_pending is None:
                cls._finish()

    @classmethod
    def _resolve_pending_cue(cls):
        """True when replay may continue past the armed cue. Otherwise waits
        cue_wait_frames, tries ONE correction (re-aim at the live target),
        waits again, then aborts."""
        cue = cls._cue_pending
        if cls._cue_satisfied(cue):
            cls._cue_pending = None
            cls._cue_corrected = False
            return True
        cls._cue_wait += 1
        if cls._cue_wait <= Toggles.Orchestrator.cue_wait_frames:
            return False
        if not cls._cue_corrected:
            cls._cue_corrected = True
            cls._cue_wait = 0
            cls.status = f"correcting: {cue[2]} {cue[3]}"
            cls._correct(cue)
            return False
        cls.abort(f"cue failed: expected {cue[2]} on '{cue[3]}' after event {cue[0]}")
        return False

    @classmethod
    def _cue_satisfied(cls, cue):
        """A live change matching the cue's anchor appeared since replay
        start (and wasn't already claimed by an earlier identical cue)."""
        _index, stack_name, kind, display_name, new_repr, _tile = cue
        stack = _stacks().get(stack_name)
        if stack is None:
            return True
        mark = cls._replay_marks.get(stack_name, 0)
        for change in reversed(stack.history):
            if change.group_id <= mark:
                break
            if id(change) in cls._matched_ids:
                continue
            if type(change).__name__ != kind or str(change.display_name) != display_name:
                continue
            if Toggles.Orchestrator.cue_match_values and _short_repr(change.new) != new_repr:
                continue
            cls._matched_ids.add(id(change))
            return True
        return False

    @classmethod
    def _correct(cls, cue):
        """Re-aim: the recorded click missed because the target moved since
        recording. Resolve the cue's draw_state LIVE (tile repr against the
        cache, the CaretLocation trick) and replay the last press at its
        current center; no live target -> re-press at the recorded spot."""
        input_id, x, y = cls._last_click or ("left_mouse", cls._virtual_x, cls._virtual_y)
        tile_repr = cue[5]
        cache = getattr(Melty, "cache", None)
        if cache is not None and tile_repr and tile_repr != "None":
            for tile_id, draw_state in cache.key_to_draw_state.items():
                if repr(tile_id)[:200] == tile_repr and draw_state is not None:
                    live_left = getattr(draw_state, "abs_left", None)
                    live_top = getattr(draw_state, "abs_top", None)
                    if live_left is not None and live_top is not None:
                        x = live_left + (getattr(draw_state, "width", 0) or 0) / 2.0
                        y = live_top + (getattr(draw_state, "height", 0) or 0) / 2.0
                    break
        cls._inject((0.0, "move", x, y))
        cls._inject((0.0, "down", input_id, x, y))
        cls._inject((0.0, "up", input_id, x, y))

    @classmethod
    def _inject(cls, event):
        _dt, kind = event[0], event[1]
        handler = Melty.event_handler
        cls._injecting = True
        try:
            if kind == "move":
                x, y = event[2], event[3]
                cls._virtual_x, cls._virtual_y = x, y
                handler.feed_move(x, y)
            elif kind == "down":
                input_id, x, y = event[2], event[3], event[4]
                cls._virtual_x, cls._virtual_y = x, y
                if input_id in _IMGUI_BUTTON:
                    cls._virtual_buttons[input_id] = True
                    cls._last_click = (input_id, x, y)
                handler.feed_down(input_id, x, y)
            elif kind == "up":
                input_id, x, y = event[2], event[3], event[4]
                cls._virtual_x, cls._virtual_y = x, y
                cls._virtual_buttons.pop(input_id, None)
                handler.feed_up(input_id, x, y)
            elif kind == "change":
                input_id, value = event[2], event[3]
                if input_id == "scroll_y":
                    cls._virtual_wheel += value
                handler.feed_change(input_id, value)
            elif kind == "key":
                key, mods = event[2], event[3]
                cls._virtual_mods = mods
                handler.set_modifiers(
                    shift=bool(mods & glfw.MOD_SHIFT), ctrl=bool(mods & glfw.MOD_CONTROL),
                    alt=bool(mods & glfw.MOD_ALT), meta=bool(mods & glfw.MOD_SUPER))
                Melty.frame_key_events.append((key, mods))
            elif kind == "char":
                cls._virtual_chars.append(event[2])
        finally:
            cls._injecting = False

    @classmethod
    def stamp_io(cls, io):
        """From SplitOverlayRenderer.process_inputs (frame start): while a
        replay drives, the virtual state replaces the real pointer/keys for
        imgui — the real ones were muted at the handler funnel."""
        if cls.replaying is None:
            return
        io.mouse_pos = (cls._virtual_x, cls._virtual_y)
        for input_id, index in _IMGUI_BUTTON.items():
            io.mouse_down[index] = bool(cls._virtual_buttons.get(input_id))
        mods = cls._virtual_mods
        io.key_shift = bool(mods & glfw.MOD_SHIFT)
        io.key_ctrl = bool(mods & glfw.MOD_CONTROL)
        io.key_alt = bool(mods & glfw.MOD_ALT)
        io.key_super = bool(mods & glfw.MOD_SUPER)
        if cls._virtual_wheel:
            io.mouse_wheel = cls._virtual_wheel
            cls._virtual_wheel = 0.0
        for codepoint in cls._virtual_chars:
            io.add_input_character(codepoint)
        cls._virtual_chars = []

    @classmethod
    def _release_virtual(cls):
        """Feed an UP for every virtually-held button so the handler never
        latches a phantom drag past the replay."""
        handler = Melty.event_handler
        cls._injecting = True
        try:
            for input_id in list(cls._virtual_buttons):
                handler.feed_up(input_id, cls._virtual_x, cls._virtual_y)
        finally:
            cls._injecting = False
        cls._virtual_buttons = {}
        cls._virtual_wheel = 0.0
        cls._virtual_chars = []
        cls._virtual_mods = 0

    @classmethod
    def _finish(cls):
        orchestration = cls.replaying
        cls.replaying = None
        cls._cue_pending = None
        cls._release_virtual()
        cls.status = ""
        if orchestration.restore_on_finish:
            cls._restore_steps = [(name, cls._replay_marks.get(name, 0))
                                  for name in _stacks()]
            cls.status = "restoring"
        else:
            notify(f"Orchestration '{orchestration.name}' finished",
                   tint=(0.5, 0.9, 0.5, 1.0), tag="orchestrator")
        request_render()

    @classmethod
    def abort(cls, reason):
        """Stop a replay in place — no restore, the app stays as it is (a
        partial restore over an unverified state is scarier than an honest
        stop)."""
        if cls.replaying is None and not cls._restore_steps:
            return
        cls.replaying = None
        cls._cue_pending = None
        cls._restore_steps = []
        cls._release_virtual()
        cls.status = ""
        notify(f"Replay stopped: {reason}", tint=(0.95, 0.6, 0.3, 1.0),
               tag="orchestrator", urgent=True)
        request_render()

    @classmethod
    def _step_restore(cls):
        """One undo group per frame: undo writes land through
        Melty.undo_requests on the NEXT rendered frame, so popping the whole
        span at once would overwrite same-target requests."""
        name, mark = cls._restore_steps[0]
        stack = _stacks()[name]
        if stack.history and stack.history[-1].group_id > mark:
            if name == "edits":
                UndoManager.undo()
            else:
                NavUndo.undo()
            return
        cls._restore_steps.pop(0)
        if not cls._restore_steps:
            cls.status = ""
            notify("Orchestration finished — state restored",
                   tint=(0.5, 0.9, 0.5, 1.0), tag="orchestrator")

    @classmethod
    def hotkey(cls):
        """Ctrl+Shift+O: stop whatever is running (recording -> stop + trim
        the hotkey's own keystrokes; replaying/restoring -> abort)."""
        if cls.recording is not None:
            cls.stop_recording(via_hotkey=True)
        elif cls.replaying is not None or cls._restore_steps:
            cls.abort("hotkey")


# ── the window ───────────────────────────────────────────────────────────

_window_draw_state = None
_last_signature = None


def orchestrator_sync():
    """Once per frame from the always-rendering root (beside fast_dock_sync):
    cue capture while recording, and a repaint of the window's cached tile
    whenever engine state or the collection changed outside a hovered
    frame."""
    global _last_signature
    Orchestrator.poll_recording()
    root = getattr(Melty.vis, "root", None)
    store = getattr(root, "orchestrations", None)
    rows = tuple((key, orchestration.name, len(orchestration.events),
                  orchestration.restore_on_finish)
                 for key, orchestration in store.orchestrations.items()) if store else ()
    signature = (Orchestrator.status, id(Orchestrator.recording),
                 id(Orchestrator.replaying), Orchestrator._replay_index,
                 bool(Orchestrator._restore_steps), rows)
    if signature != _last_signature:
        _last_signature = signature
        if _window_draw_state is not None and Melty.cache is not None \
                and _window_draw_state._tile_id is not None:
            Melty.cache.invalidate_up(_window_draw_state._tile_id, force=True)
        request_render()


@window(input_value=None, tint=(0.719, 0.478, 0.208), icon=f"",
        display_name="Orchestrator", initial={"width": 430, "height": 340})
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True, tint=(0.719, 0.478, 0.208))
def draw_orchestrator(input_value=None, draw_state=None, style_manager=None,
                      selected_key="", left_mouse_down=False, **kwargs):
    global _window_draw_state
    _window_draw_state = draw_state
    root = getattr(Melty.vis, "root", None)
    store = getattr(root, "orchestrations", None)
    if store is None:
        imgui.text("model not loaded")
        return False, input_value
    orchestrations = store.orchestrations

    # ---- icons (glyph literals - the editor renders them as a font) ----
    record_icon = f""
    stop_icon = f""
    play_icon = f""
    plus_icon = f""
    delete_icon = f""
    restore_icon = f""

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
    selected_bg_value = 0.14
    factor, saturation = 0.85, 1.0
    button_bg_value, button_text_value = 0.13, 1.25
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8
    dim_text_value = 0.45
    record_tint = (0.85, 0.25, 0.25)              # armed recording button / pulse
    restore_on_tint = (0.4, 0.85, 0.5)            # restore chip when checked

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(28.0)
    row_gap = px(3.0)
    toolbar_height = px(26.0)
    toolbar_gap = px(8.0)
    button_pad_x = px(10.0)
    button_gap = px(6.0)
    chip_width = px(24.0)                          # per-row icon buttons (play/restore/delete)
    corner = px(6.0)
    pad_x = px(8.0)
    text_nudge_y = px(-1.0)
    status_height = px(20.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 300)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    changed = False

    # The engine trims the stop click off a take's tail with this rect.
    Orchestrator.window_rect = (draw_state.abs_left, draw_state.abs_top,
                                draw_state.abs_left + (draw_state.width or content_width),
                                draw_state.abs_top + (draw_state.height or 200))

    def _mix(tint, value, mix_factor, mix_saturation):
        color = tint if (isinstance(tint, tuple) and len(tint) >= 3) else (0.5, 0.5, 0.5)
        return style_manager.make_color_rgb(color[0], color[1], color[2], value=value,
                                            factor=mix_factor, saturation_scale=mix_saturation)

    def _u32(color, alpha=1.0):
        return imgui.get_color_u32_rgba(color[0], color[1], color[2], alpha)

    def _in(rect, x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def _button(x, y, width, height, label, tint, active=False, icon_only=False):
        """Draw a toolbar/chip button, return (clicked, right_edge)."""
        rect = (x, y, x + width, y + height)
        hovered = hover_ok and _in(rect, mouse_x, mouse_y)
        bg_value = button_bg_value + (0.08 if active else 0.0) \
            + (hover_bg_boost if hovered else 0.0)
        background = _mix(tint, bg_value, factor, saturation)
        text_color = _mix(tint, button_text_value + (hover_text_boost if hovered else 0.0),
                          factor, text_saturation)
        if active or hovered or not icon_only:
            add_shadow((x, y, width, height), corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(rect[0], rect[1], rect[2], rect[3],
                                      _u32(background), rounding=corner)
        label_size = imgui.calc_text_size(label)
        draw_list.add_text(x + (width - label_size[0]) / 2.0,
                           y + (height - label_size[1]) / 2.0 + text_nudge_y,
                           _u32(text_color), label)
        clicked = click is not None and _in(rect, click[0], click[1])
        return clicked, rect[2]

    # ---- toolbar: Record/Stop - Play/Abort - + New ----
    toolbar_x = origin_x + pad_x
    toolbar_y = origin_y
    engine_busy = Orchestrator.replaying is not None or bool(Orchestrator._restore_steps)
    is_recording = Orchestrator.recording is not None

    record_label = f"{stop_icon}  Stop" if is_recording else f"{record_icon}  Record"
    record_width = imgui.calc_text_size(record_label)[0] + 2 * button_pad_x
    record_clicked, edge = _button(toolbar_x, toolbar_y, record_width, toolbar_height,
                                   record_label, record_tint if is_recording else draw_state.tint,
                                   active=is_recording)
    if record_clicked and not engine_busy:
        if is_recording:
            Orchestrator.stop_recording()
        else:
            target = orchestrations.get(selected_key)
            if target is None:
                from src.lsd.gl_gui.model.app_model import Orchestration
                target = Orchestration()
                target.name = f"Orchestration {len(orchestrations) + 1}"
                orchestrations[target.id] = target
                draw_state.selected_key = target.id       # auto-select: persists
            Orchestrator.start_recording(target)
        changed = True

    if engine_busy:
        abort_label = f"{stop_icon}  Abort"
        abort_width = imgui.calc_text_size(abort_label)[0] + 2 * button_pad_x
        abort_clicked, edge = _button(edge + button_gap, toolbar_y, abort_width,
                                      toolbar_height, abort_label, record_tint, active=True)
        if abort_clicked:
            Orchestrator.abort("stop button")

    new_label = f"{plus_icon}  New"
    new_width = imgui.calc_text_size(new_label)[0] + 2 * button_pad_x
    new_clicked, edge = _button(edge + button_gap, toolbar_y, new_width, toolbar_height,
                                new_label, draw_state.tint)
    if new_clicked and not is_recording and not engine_busy:
        from src.lsd.gl_gui.model.app_model import Orchestration
        fresh = Orchestration()
        fresh.name = f"Orchestration {len(orchestrations) + 1}"
        orchestrations[fresh.id] = fresh
        draw_state.selected_key = fresh.id
        changed = True

    # ---- rows ----
    rows_top = toolbar_y + toolbar_height + toolbar_gap
    row_left = origin_x + pad_x
    row_right = origin_x + content_width - pad_x
    row_stride = row_height + row_gap
    row_y = rows_top

    for key, orchestration in list(orchestrations.items()):
        row_rect = (row_left, row_y, row_right, row_y + row_height)
        if clip is not None and (row_rect[3] < clip[1] or row_rect[1] > clip[3]):
            row_y += row_stride
            continue
        tint = orchestration.tint if isinstance(orchestration.tint, tuple) else draw_state.tint
        is_selected = key == selected_key
        is_playing = Orchestrator.replaying is orchestration
        is_take = Orchestrator.recording is orchestration
        row_hovered = hover_ok and _in(row_rect, mouse_x, mouse_y)

        bg_value = (selected_bg_value if is_selected else row_bg_value) \
            + (hover_bg_boost if row_hovered else 0.0)
        if is_selected:
            add_shadow((row_rect[0], row_rect[1], row_right - row_left, row_height),
                       corner_radius=corner, clip=clip)
        draw_list.add_rect_filled(row_rect[0], row_rect[1], row_rect[2], row_rect[3],
                                  _u32(_mix(tint, bg_value, factor, saturation)),
                                  rounding=corner)

        # play chip (left)
        play_clicked, chip_edge = _button(row_left + px(4), row_y + (row_height - px(22)) / 2.0,
                                          chip_width, px(22),
                                          play_icon, tint, icon_only=True)
        if play_clicked and not is_recording and not engine_busy:
            draw_state.selected_key = key
            Orchestrator.play(orchestration)
            changed = True

        # name + state
        text_x = chip_edge + px(8)
        text_value = row_text_value + (hover_text_boost if row_hovered else 0.0)
        name_color = _mix((record_tint if is_take else tint), text_value, factor, text_saturation)
        label = orchestration.name + ("  (recording)" if is_take else "")
        draw_list.add_text(text_x, row_y + (row_height - imgui.get_text_line_height()) / 2.0
                           + text_nudge_y, _u32(name_color), label)

        # right side: delete - restore chip - tag
        delete_left = row_right - px(4) - chip_width
        if row_hovered and not is_take and not is_playing:
            delete_clicked, _ = _button(delete_left, row_y + (row_height - px(22)) / 2.0,
                                        chip_width, px(22), delete_icon, tint, icon_only=True)
            if delete_clicked:
                orchestrations.pop(key, None)
                changed = True
                row_y += row_stride
                continue
        restore_left = delete_left - button_gap - chip_width
        restore_tint = restore_on_tint if orchestration.restore_on_finish else tint
        restore_clicked, _ = _button(restore_left, row_y + (row_height - px(22)) / 2.0,
                                     chip_width, px(22), restore_icon, restore_tint,
                                     active=orchestration.restore_on_finish, icon_only=True)
        if restore_clicked:
            orchestration.restore_on_finish = not orchestration.restore_on_finish
            changed = True

        if is_playing:
            tag = f"{Orchestrator._replay_index}/{len(orchestration.events)}"
        else:
            tag = f"{orchestration.duration:g}s · {len(orchestration.events)} ev"
        tag_color = _mix(tint, dim_text_value, factor, text_saturation)
        tag_width = imgui.calc_text_size(tag)[0]
        draw_list.add_text(restore_left - button_gap - tag_width,
                           row_y + (row_height - imgui.get_text_line_height()) / 2.0
                           + text_nudge_y, _u32(tag_color), tag)

        # row click (outside the chips): select
        if click is not None and _in(row_rect, click[0], click[1]) \
                and click[0] < restore_left - button_gap - tag_width:
            if selected_key != key:
                draw_state.selected_key = key
                changed = True
        row_y += row_stride

    if not orchestrations:
        empty_color = _mix(draw_state.tint, dim_text_value, factor, text_saturation)
        draw_list.add_text(row_left + px(4), rows_top + px(4), _u32(empty_color),
                           "No orchestrations — press Record to capture one.")
        row_y += row_stride

    # ---- status footer ----
    if Orchestrator.status:
        status_color = _mix(record_tint if is_recording else draw_state.tint,
                            row_text_value, factor, text_saturation)
        draw_list.add_text(row_left + px(4), row_y + px(2), _u32(status_color),
                           Orchestrator.status)
        row_y += status_height

    imgui.dummy(content_width, max(1.0, (row_y - origin_y) + row_gap))
    return changed, input_value


# The engine sees every real input through this one registration (a tap).
set_input_tap(Orchestrator.tap)

# Ctrl+Shift+O anywhere: stop a recording / abort a replay - the mouse is
# busy driving (or being driven), so this must not depend on the window.
Melty.register_global_hotkey(glfw.KEY_O, glfw.MOD_CONTROL | glfw.MOD_SHIFT,
                             Orchestrator.hotkey, text_focus_ok=True)
