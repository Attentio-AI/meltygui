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
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.core_undo import (UndoManager, NavUndo,
                                                      WindowChange, WindowMoveChange)
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


# Legacy cue tuples (recorded before cues became dicts) read from this
# position map; every cue field access goes through cue_get so old takes
# keep replaying. New cues are dicts (see make_cue) - serializable, and
# extensible without another positional migration.
_CUE_TUPLE_FIELDS = {"at": 0, "stack": 1, "kind": 2, "name": 3,
                     "new_repr": 4, "tile": 5, "anchor": 6}


def cue_get(cue, field, default=None):
    if isinstance(cue, dict):
        return cue.get(field, default)
    index = _CUE_TUPLE_FIELDS.get(field)
    if index is not None and len(cue) > index:
        return cue[index]
    return default


def _primitive_or_repr(value):
    """Cue values: numeric/str/bool primitives kept RAW (the servo computes
    gain from them; text verification compares exactly), everything else a
    repr string."""
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return _short_repr(value)


def make_cue(change, stack_name, event_index, take):
    """The enriched cue: effect signature (kind/name/old/new/direction),
    addressing (chain — maximal capture, minimal_path trims late; tile),
    geometry (anchor window + the target leaf's rect and the press point as
    a FRACTION of it — what re-targets a take onto a sibling field), and the
    applicability signature (editor + value_type — what licenses reusing
    this take on a different field of the same widget kind)."""
    from src.lsd.gl_gui.view.playground.selectors import name_chain
    ds = getattr(change, "draw_state", None)
    anchor = _cue_anchor(change)
    leaf_rect = None
    press_frac = None
    if ds is not None:
        left = getattr(ds, "abs_left", None)
        top = getattr(ds, "abs_top", None)
        width = getattr(ds, "width", 0) or 0
        height = getattr(ds, "height", 0) or 0
        if left is not None and top is not None and anchor is not None:
            leaf_rect = (float(left - anchor[2]), float(top - anchor[3]),
                         float(width), float(height))
        # last recorded press decides the fraction; a keyboard-only edit has
        # no press so the executors default to the leaf's center
        press = next((event for event in reversed(take.events)
                      if event[1] == "down" and len(event) == 5), None)   # absolute only
        if press is not None and left is not None and width > 0 and height > 0:
            press_frac = (max(0.0, min(1.0, (press[3] - left) / width)),
                          max(0.0, min(1.0, (press[4] - top) / height)))
    view_func = getattr(ds, "_view_func", None) if ds is not None else None
    return {
        "at": event_index,
        "stack": stack_name,
        "kind": type(change).__name__,
        "name": str(change.display_name),
        "chain": list(name_chain(ds)) if ds is not None else [],
        "old": _primitive_or_repr(change.old),
        "new": _primitive_or_repr(change.new),
        "new_repr": _short_repr(change.new),
        "direction": getattr(change, "direction", None),
        "editor": getattr(view_func, "__name__", None),
        "value_type": type(change.new).__name__,
        "anchor": anchor,
        "leaf_rect": leaf_rect,
        "press_frac": press_frac,
        "tile": repr(getattr(ds, "_tile_id", None))[:200],
    }


def _cue_anchor(change):
    """The window a cue's events reference: (window_tile_repr, window_name,
    abs_left, abs_top) of the change's PARENT WINDOW at cue-cut time, or None
    (no draw_state / no enclosing window — those events stay absolute).
    A window-level change (open/close, move) anchors on the window itself;
    a WindowMoveChange anchors at the window's PRE-drag position — the
    events that produced the cue (the drag) happened before the move landed,
    so replay must re-reference them against where the window STARTED."""
    ds = getattr(change, "draw_state", None)
    if ds is None:
        return None
    if isinstance(change, (WindowChange, WindowMoveChange)):
        window_ds = ds
    else:
        window_ds = getattr(ds, "parent_window", None)
    if window_ds is None:
        return None
    left = getattr(window_ds, "abs_left", None)
    top = getattr(window_ds, "abs_top", None)
    if left is None or top is None:
        return None
    if isinstance(change, WindowMoveChange):
        left -= change.new[0] - change.old[0]
        top -= change.new[1] - change.old[1]
    return (repr(getattr(window_ds, "_tile_id", None))[:200],
            str(getattr(window_ds, "name", "?")), float(left), float(top))


def _key_label(key, mods=0):
    """Human name for a glfw key (+held modifiers): printable GLFW codes ARE
    ASCII, the rest come from the backend's name table."""
    from src.lsd.gl_gui.events.event_backends import ImGuiBackend
    if 32 <= key < 127:
        name = chr(key)
    else:
        name = ImGuiBackend.KEY_NAMES.get(key, f"key_{key}")
    prefix = "".join(part for bit, part in ((glfw.MOD_CONTROL, "ctrl+"),
                                            (glfw.MOD_SHIFT, "shift+"),
                                            (glfw.MOD_ALT, "alt+"),
                                            (glfw.MOD_SUPER, "super+"))
                     if mods & bit)
    return prefix + name


def group_events(events, cues=()):
    """Collapse a take's raw event stream into readable rows for the window's
    event list. Returns [(start_index, end_index, kind, label, tag)] where
    [start_index, end_index) is the run of raw events the row covers (the
    replay progress highlight reads it) and kind is one of "move", "click",
    "drag", "down", "up", "type", "key", "scroll", "cue".

    Grouping: a run of moves is one row; a down whose matching up follows
    with only moves between is a CLICK (no moves) or a DRAG; consecutive
    chars are one typed string; consecutive same-key presses fold with a
    ×count; scroll deltas on one axis sum. Runs SPLIT at any cue's
    event_index so cue rows land exactly between the events they gate."""
    boundaries = {cue_get(cue, "at", 0) for cue in cues}
    cues_at = {}
    for cue in cues:
        cues_at.setdefault(cue_get(cue, "at", 0), []).append(cue)

    def _emit_cues(rows, index):
        for cue in cues_at.get(index, ()):
            anchor = cue_get(cue, "anchor")
            rows.append((index, index, "cue",
                         f"cue: {cue_get(cue, 'kind')} '{cue_get(cue, 'name')}'"
                         + (f" @ {anchor[1]}" if anchor else ""),
                         cue_get(cue, "stack", "")))

    def _run_end(start, predicate):
        """End of the run at `start` (exclusive), stopping at cue boundaries."""
        end = start + 1
        while end < len(events) and end not in boundaries and predicate(events[end]):
            end += 1
        return end

    rows = []
    i = 0
    while i < len(events):
        _emit_cues(rows, i)
        event = events[i]
        dt, kind = event[0], event[1]
        if kind == "move":
            end = _run_end(i, lambda ev: ev[1] == "move")
            first, last = events[i], events[end - 1]
            rows.append((i, end, "move",
                         f"move ({first[2]:.0f}, {first[3]:.0f}) → ({last[2]:.0f}, {last[3]:.0f})",
                         f"{end - i}× · {last[0] - first[0]:.1f}s"))
            i = end
        elif kind == "down":
            input_id = event[2]
            # matching up with only MOVES between (and no cue splitting it)
            end = _run_end(i, lambda ev: ev[1] == "move")
            if end < len(events) and end not in boundaries \
                    and events[end][1] == "up" and events[end][2] == input_id:
                up = events[end]
                move_count = end - i - 1
                if move_count == 0:
                    rows.append((i, end + 1, "click",
                                 f"click {input_id} @ ({event[3]:.0f}, {event[4]:.0f})",
                                 f"{dt:.1f}s"))
                else:
                    rows.append((i, end + 1, "drag",
                                 f"drag {input_id} ({event[3]:.0f}, {event[4]:.0f})"
                                 f" → ({up[3]:.0f}, {up[4]:.0f})",
                                 f"{move_count} moves · {up[0] - dt:.1f}s"))
                i = end + 1
            else:
                rows.append((i, i + 1, "down",
                             f"down {input_id} @ ({event[3]:.0f}, {event[4]:.0f})",
                             f"{dt:.1f}s"))
                i += 1
        elif kind == "up":
            rows.append((i, i + 1, "up",
                         f"up {event[2]} @ ({event[3]:.0f}, {event[4]:.0f})",
                         f"{dt:.1f}s"))
            i += 1
        elif kind == "char":
            end = _run_end(i, lambda ev: ev[1] == "char")
            text = "".join(chr(ev[2]) for ev in events[i:end])
            rows.append((i, end, "type", f'type "{text}"', f"{dt:.1f}s"))
            i = end
        elif kind == "key":
            key, mods = event[2], event[3]
            end = _run_end(i, lambda ev: ev[1] == "key"
                           and ev[2] == key and ev[3] == mods)
            count = end - i
            label = f"key {_key_label(key, mods)}"
            rows.append((i, end, "key",
                         label + (f" ×{count}" if count > 1 else ""), f"{dt:.1f}s"))
            i = end
        elif kind == "change":
            input_id = event[2]
            end = _run_end(i, lambda ev: ev[1] == "change" and ev[2] == input_id)
            total = sum(ev[3] for ev in events[i:end])
            rows.append((i, end, "scroll",
                         f"{input_id} {total:+.1f}", f"{dt:.1f}s"))
            i = end
        else:
            rows.append((i, i + 1, kind, f"{kind} {event[2:]!r}", f"{dt:.1f}s"))
            i += 1
    _emit_cues(rows, len(events))
    # trailing cues after the last event (recorded in the take's tail)
    for index in sorted(cues_at):
        if index > len(events):
            _emit_cues(rows, index)
    return rows


# editor -> command verb for the generalized view (mirrors change_value's
# archetype map; kept separate so the window never imports the task module).
_COMMAND_VERBS = {"draw_float": "drag", "draw_int": "drag",
                  "draw_str": "type", "draw_text": "type", "draw_bool": "toggle"}


def _format_value(value):
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def generalized_commands(take):
    """The take read at the COMMAND level: one row per cue — the effects ARE
    the generalization (events are the how, cues the what). A leaf-editor
    cue renders as the change_value call that reproduces it; container bool
    flips as expand/collapse; window and nav cues by their display names.
    Rows share group_events' (start_index, end_index, kind, label, tag)
    shape so the window's detail renderer and the replay-progress highlight
    serve both tabs."""
    rows = []
    previous = 0
    for cue in (getattr(take, "cues", None) or []):
        at = max(cue_get(cue, "at", 0), previous)
        kind = cue_get(cue, "kind")
        name = cue_get(cue, "name") or "?"
        chain = cue_get(cue, "chain") or []
        path = "/".join(chain[-2:]) if len(chain) >= 2 else name
        new = cue_get(cue, "new", cue_get(cue, "new_repr"))
        anchor = cue_get(cue, "anchor")
        tag = anchor[1] if anchor else (cue_get(cue, "stack") or "")
        if kind == "Change":
            verb = _COMMAND_VERBS.get(cue_get(cue, "editor"))
            if verb is not None:
                label = f'change_value("{path}", {_format_value(new)})'
            elif cue_get(cue, "value_type") == "bool":
                verb = "expand"
                label = (f'expand "{path}"' if new in (True, "True")
                         else f'collapse "{path}"')
            else:
                verb = "set"
                label = f"set {path} = {_format_value(new)}"
        elif kind == "WindowMoveChange":
            verb, label = "move", name           # display_name is "move <window>"
        elif kind == "WindowChange":
            verb, label = "window", name         # "open <w>" or "close <w>"
        else:
            verb, label = "goto", name
        rows.append((previous, at, verb, label, tag))
        previous = at
    return rows


class OrchestratorPanelState(DictConversion):
    """Per-window UI state (injected like TabState): which orchestrations are
    EXPANDED (several at once) and which detail tab each shows — "commands"
    (the generalized view, the default) or "events" (the raw grouped
    stream). Keyed by orchestration id; persisted with the window."""

    def __init__(self):
        super().__init__()
        self.expanded = {}          # orchestration id -> True
        self.tab = {}               # orchestration id -> "commands" | "events"


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
    _record_base_marks = {}     # stack name -> group id at record START (restore target;
                                # _record_marks advances with every cue sweep)
    _relativized_upto = 0       # events before this index are claimed by a cue's anchor

    _replay_t0 = 0.0
    _replay_index = 0           # next event to inject
    _replay_marks = {}          # stack name -> group id at replay start (restore + restore baseline)
    _cue_cursor = 0             # next cue (index into orch.cues) not yet armed
    _cue_pending = None         # armed cue awaiting verification
    _cue_wait = 0
    _cue_corrected = False
    _matched_ids = set()        # id(change) of live changes already claimed by a cue
    _anchor_cache = {}          # cue_index -> (left, top): resolved anchor origins this replay
    _click_seq = 0              # unique keys for click-ripple emphasis notes
    _last_click = None          # (input_id, x, y) of the last injected press
    _injecting = False          # True while pump feeds the handler (tap lets those through)
    _restore_steps = []         # [(stack_name, mark_gid), ...] still to unwind
    _pending_release = set()    # buttons held at record start (the Record click)
    _task = None                # active ValueTask (change_value) — generator, stepped per frame

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
        if cls.replaying is not None or cls._restore_steps or cls._task is not None:
            if kind == "key" and args and args[0] == glfw.KEY_ESCAPE:
                cls.abort("Esc")
            # A real RELEASE must reach the handler: the play click's own
            # release lands after the replay armed, and we replay it latched
            # that button down - every subsequent move then read as a
            # left-drag from the play button. Only a button the replay is
            # VIRTUALLY holding keeps its real release muted (a passed-through
            # up would cut the injected drag mid-gesture).
            if kind == "up" and args and args[0] not in cls._virtual_buttons:
                return False
            return True                       # mute real input while driving
        take = cls.recording
        if take is not None:
            # The click that pressed Record is still HELD when recording arms:
            # the drag/moves and release belong to the arming gesture, not the
            # take (recorded, but replayed as an immediate click-drag).
            # Swallow moves and those buttons' releases until every armed-down
            # button is up - keys/chars typed meanwhile are real content.
            if cls._pending_release:
                if kind == "up" and args and args[0] in cls._pending_release:
                    cls._pending_release.discard(args[0])
                    return False
                if kind == "move":
                    return False
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
        # Buttons already down when recording arms (the Record click itself)
        # - their remaining drag is swallowed by the tap until released.
        handler = Melty.event_handler
        cls._pending_release = {input_id for input_id in _IMGUI_BUTTON
                                if handler is not None
                                and getattr(handler, "is_down", None)
                                and handler.is_down(input_id)}
        cls._record_marks = {name: stack._next_group_id
                             for name, stack in _stacks().items()}
        cls._record_base_marks = dict(cls._record_marks)
        cls._relativized_upto = 0
        cls.status = "recording"
        request_render()

    @classmethod
    def stop_recording(cls, via_hotkey=False):
        take = cls.recording
        cls.recording = None
        cls._pending_release = set()
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
        # A take recorded WITH restore resets the app right here: everything
        # the recording session put on the undo stacks (edits, window
        # opens/closes, window moves) unwinds back to the session-start marks
        # - same history as the post-replay restore, so what you see after
        # Stop is exactly what a subsequent replay will leave behind.
        if getattr(take, "restore_on_finish", False):
            cls._restore_steps = [(name, cls._record_base_marks.get(name, 0))
                                  for name in _stacks()]
            cls.status = "restoring"
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
                take.cues.append(make_cue(change, name, len(take.events), take))
                # The cue's parent window re-references the mouse events that
                # LED to it: absolute → window-relative, so replay can follow
                # the window wherever it sits now. Events after the last cue
                # keep absolute coordinates - the fallback.
                cls._relativize_events(take, len(take.events), len(take.cues) - 1)
                mark = max(mark, gid)
            cls._record_marks[name] = max(cls._record_marks.get(name, 0), mark)

    @classmethod
    def _relativize_events(cls, take, upto_index, cue_index):
        """Rewrite the mouse events in [_relativized_upto, upto_index) as
        window-relative against cue `cue_index`'s anchor: a trailing
        cue-index element marks the format —
          move: (dt, "move", x, y)            → (dt, "move", rx, ry, cue_index)
          down/up: (dt, kind, id, x, y)       → (dt, kind, id, rx, ry, cue_index)
        No anchor on the cue → the range stays absolute (and stays claimed,
        so a later cue can't re-reference another window's events)."""
        anchor = cue_get(take.cues[cue_index], "anchor")
        start = cls._relativized_upto
        cls._relativized_upto = max(cls._relativized_upto, upto_index)
        if anchor is None:
            return
        _tile, _name, anchor_x, anchor_y = anchor
        for i in range(start, min(upto_index, len(take.events))):
            event = take.events[i]
            if event[1] == "move" and len(event) == 4:
                take.events[i] = (event[0], "move", event[2] - anchor_x,
                                  event[3] - anchor_y, cue_index)
            elif event[1] in ("down", "up") and len(event) == 5:
                take.events[i] = (event[0], event[1], event[2],
                                  event[3] - anchor_x, event[4] - anchor_y, cue_index)

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
        cls._anchor_cache = {}
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
        if cls._task is not None:
            cls._assert_cursor()
            cls._step_task()
            request_render()
            return
        orchestration = cls.replaying
        if orchestration is None:
            return
        cls._assert_cursor()
        request_render()
        if cls._cue_pending is not None and not cls._resolve_pending_cue():
            return
        speed = max(0.05, Toggles.Orchestrator.replay_speed)
        now = (time.monotonic() - cls._replay_t0) * speed
        events = orchestration.events
        cues = orchestration.cues
        while cls._replay_index < len(events):
            if cls._cue_cursor < len(cues) \
                    and cue_get(cues[cls._cue_cursor], "at", 0) <= cls._replay_index:
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
            cls.status = f"correcting: {cue_get(cue, 'kind')} {cue_get(cue, 'name')}"
            cls._correct(cue)
            return False
        cls.abort(f"cue failed: expected {cue_get(cue, 'kind')} on "
                  f"'{cue_get(cue, 'name')}' after event {cue_get(cue, 'at')}")
        return False

    @classmethod
    def _cue_satisfied(cls, cue):
        """A live change matching the cue's anchor appeared since replay
        start (and wasn't already claimed by an earlier identical cue)."""
        stack_name, kind = cue_get(cue, "stack"), cue_get(cue, "kind")
        display_name, new_repr = cue_get(cue, "name"), cue_get(cue, "new_repr")
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
        tile_repr = cue_get(cue, "tile")
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
    def _anchor_origin(cls, cue_index):
        """Live (left, top) of the window a relativized event references —
        resolved ONCE per cue per replay (a window that moves mid-replay must
        not shift the events recorded against where it stood). Resolution:
        the cue's window tile against the live cache (nested windows too),
        then the registered window by name; both missing → the RECORDED
        origin, which reproduces the original absolute coordinates."""
        cached = cls._anchor_cache.get(cue_index)
        if cached is not None:
            return cached
        origin = (0.0, 0.0)
        cues = cls.replaying.cues if cls.replaying is not None else []
        anchor = (cue_get(cues[cue_index], "anchor")
                  if 0 <= cue_index < len(cues) else None)
        if anchor is not None:
            tile_repr, window_name, recorded_left, recorded_top = anchor
            origin = (recorded_left, recorded_top)
            window_ds = None
            cache = getattr(Melty, "cache", None)
            if cache is not None and tile_repr and tile_repr != "None":
                for tile_id, draw_state in cache.key_to_draw_state.items():
                    if repr(tile_id)[:200] == tile_repr and draw_state is not None:
                        window_ds = draw_state
                        break
            if window_ds is None:
                managed = Melty.registered_windows.get(window_name)
                window_ds = getattr(managed, "draw_state", None)
            if window_ds is not None:
                live_left = getattr(window_ds, "abs_left", None)
                live_top = getattr(window_ds, "abs_top", None)
                if live_left is not None and live_top is not None:
                    origin = (live_left, live_top)
        cls._anchor_cache[cue_index] = origin
        return origin

    @classmethod
    def _event_xy(cls, event, x_slot):
        """An event's cursor position in SCREEN coordinates: relativized
        events (trailing cue-index element) shift by their anchor's live
        origin, absolute ones pass through."""
        x, y = event[x_slot], event[x_slot + 1]
        if len(event) > x_slot + 2:
            left, top = cls._anchor_origin(event[x_slot + 2])
            return x + left, y + top
        return x, y

    @classmethod
    def _inject(cls, event):
        _dt, kind = event[0], event[1]
        handler = Melty.event_handler
        cls._injecting = True
        try:
            if kind == "move":
                x, y = cls._event_xy(event, 2)
                cls._virtual_x, cls._virtual_y = x, y
                handler.feed_move(x, y)
            elif kind == "down":
                input_id = event[2]
                x, y = cls._event_xy(event, 3)
                cls._virtual_x, cls._virtual_y = x, y
                if input_id in _IMGUI_BUTTON:
                    cls._virtual_buttons[input_id] = True
                    cls._last_click = (input_id, x, y)
                    # click ripple - per-button tint, sequential key so
                    # simultaneous ripples coexist
                    ripple_tint = {"left_mouse": (1.0, 0.85, 0.3),
                                   "right_mouse": (0.45, 0.7, 1.0),
                                   "middle_mouse": (0.6, 1.0, 0.6)}[input_id]
                    cls._click_seq += 1
                    Melty.emphasize_click(f"orch-click-{cls._click_seq}", (x, y),
                                          tint=ripple_tint)
                handler.feed_down(input_id, x, y)
            elif kind == "up":
                input_id = event[2]
                x, y = cls._event_xy(event, 3)
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
        replay OR a directed task (change_value) drives, the virtual state
        replaces the real pointer/keys for imgui — the real ones were muted
        at the handler funnel."""
        if cls.replaying is None and cls._task is None:
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
    def _assert_cursor(cls):
        """Once per driving frame: hold the virtual-pointer emphasis note on
        the engine's cursor (a lease — the overlay pass force-releases it if
        the engine stops asserting, and _release_cursor fades it on finish)."""
        Melty.emphasize_cursor("orch-cursor",
                               lambda: (Orchestrator._virtual_x, Orchestrator._virtual_y))

    @classmethod
    def _release_cursor(cls):
        note = Melty.emphasis_notes.get("orch-cursor")
        if note is not None:
            Melty.emphasize_cursor("orch-cursor", note.center, hold=False)

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
        cls._release_cursor()
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
        if cls.replaying is None and not cls._restore_steps and cls._task is None:
            return
        if cls._task is not None:
            cls._task.fail(reason)
            cls._task = None
        cls.replaying = None
        cls._cue_pending = None
        cls._restore_steps = []
        cls._release_virtual()
        cls._release_cursor()
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
            notify("State restored", tint=(0.5, 0.9, 0.5, 1.0),
                   tag="orchestrator")

    # ── directed tasks (change_value) ────────────────────────────────────

    @classmethod
    def submit(cls, task):
        """Run a directed task (change_value.ValueTask): its generator is
        stepped once per frame by pump, real input muted meanwhile, same as
        a replay. One driver at a time."""
        if (cls.recording is not None or cls.replaying is not None
                or cls._restore_steps or cls._task is not None):
            task.fail("engine busy")
            return task
        task.start_frame = Melty.frame_count
        task._generator = task.run()
        cls._task = task
        cls.status = str(task)
        request_render()
        return task

    @classmethod
    def _step_task(cls):
        task = cls._task
        try:
            next(task._generator)
            cls.status = str(task)
        except StopIteration:
            cls._task = None
            cls._release_virtual()
            cls._release_cursor()
            cls.status = ""
            if task.error:
                notify(f"{task}: {task.error}", tint=(0.95, 0.6, 0.3, 1.0),
                       tag="orchestrator", urgent=True)
            else:
                notify(f"{task} done", tint=(0.5, 0.9, 0.5, 1.0), tag="orchestrator")
            task.finish()
        except Exception as error:
            cls._task = None
            cls._release_virtual()
            cls._release_cursor()
            cls.status = ""
            task.fail(f"{type(error).__name__}: {error}")
            notify(f"{task} crashed: {error}", tint=(0.95, 0.35, 0.35, 1.0),
                   tag="orchestrator", urgent=True)

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
                  len(orchestration.cues), orchestration.restore_on_finish)
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


@window(input_value=None, tint=(0.85, 0.68, 0.46), icon=f"",
        display_name="Orchestrator", initial={"width": 430, "height": 340})
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True, tint=(0.719, 0.478, 0.208))
def draw_orchestrator(input_value=None, draw_state=None, style_manager=None,
                      panel_state: OrchestratorPanelState = None,
                      selected_key="", renaming_key="", rename_text="",
                      left_mouse_down=False, left_mouse_double_clicked=False, **kwargs):
    # NOTE the click param names: "double_left_mouse_down" canonicalises
    # to the SAME (input_id, DOWN) key as left_mouse_down (DOWN has no double
    # promotion) but the per-frame name cache keeps one name per key - it
    # silently drops every plain press. DOUBLE_CLICKED is its own key.
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
    chevron_right_icon = f""
    chevron_down_icon = f""
    rename_icon = f""

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
    engine_busy = (Orchestrator.replaying is not None or bool(Orchestrator._restore_steps)
                   or Orchestrator._task is not None)
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

    # ---- rows (one per orchestration, each expands to its detail tabs) ----
    rows_top = toolbar_y + toolbar_height + toolbar_gap
    row_left = origin_x + pad_x
    row_right = origin_x + content_width - pad_x
    row_stride = row_height + row_gap
    row_y = rows_top
    detail_row_height = px(18.0)
    detail_indent = px(34.0)
    tab_height = px(20.0)
    tab_pad_x = px(8.0)
    cue_tint = (0.85, 0.65, 0.2)                     # cue / effect rows (undo anchors)
    dim_color = _mix(draw_state.tint, dim_text_value, factor, text_saturation)
    expanded_map = panel_state.expanded if panel_state is not None else {}
    tab_map = panel_state.tab if panel_state is not None else {}
    dbl = (left_mouse_double_clicked.x, left_mouse_double_clicked.y) \
        if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None

    def _start_rename(key, orchestration):
        draw_state.renaming_key = key
        draw_state.rename_text = orchestration.name
        if panel_state is not None:
            panel_state._rename_focus = True
            panel_state._rename_was_active = False
        request_render()

    def _commit_rename(orchestration):
        text = (draw_state.rename_text or "").strip()
        if text and text != orchestration.name:
            orchestration.name = text
            request_render()
            committed = True
        else:
            committed = False
        draw_state.renaming_key = ""
        return committed

    def _detail_rows(rows_list, playing):
        """Both tabs render through here — same replay-progress highlight:
        the row being replayed brightens, finished rows dim, effect rows
        (cues / commands) wear the amber."""
        nonlocal row_y
        for start_index, end_index, row_kind, label, tag in rows_list:
            if clip is None or not (row_y + detail_row_height < clip[1] or row_y > clip[3]):
                running = playing and start_index <= Orchestrator._replay_index \
                    < max(end_index, start_index + 1)
                if running:
                    label_color = _mix(draw_state.tint, row_text_value + 0.4,
                                       factor, text_saturation)   # being replayed
                elif playing and max(end_index, start_index + 1) <= Orchestrator._replay_index:
                    label_color = dim_color                        # already replayed
                elif row_kind in ("cue", "expand", "window", "move", "goto", "set"):
                    label_color = _mix(cue_tint, row_text_value, 0.4, 1.2)
                else:
                    label_color = _mix(draw_state.tint, row_text_value * 0.75,
                                       factor, text_saturation)
                draw_list.add_text(row_left + detail_indent, row_y + text_nudge_y,
                                   _u32(label_color), label)
                if tag:
                    tag_width = imgui.calc_text_size(tag)[0]
                    draw_list.add_text(row_right - px(4) - tag_width,
                                       row_y + text_nudge_y, _u32(dim_color), tag)
            row_y += detail_row_height

    for key, orchestration in list(orchestrations.items()):
        row_rect = (row_left, row_y, row_right, row_y + row_height)
        header_visible = clip is None or not (row_rect[3] < clip[1]
                                              or row_rect[1] > clip[3])
        tint = orchestration.tint if isinstance(orchestration.tint, tuple) else draw_state.tint
        is_selected = key == selected_key
        is_playing = Orchestrator.replaying is orchestration
        is_take = Orchestrator.recording is orchestration
        is_expanded = bool(expanded_map.get(key))
        renaming = renaming_key == key
        row_hovered = hover_ok and _in(row_rect, mouse_x, mouse_y)
        chip_y = row_y + (row_height - px(22)) / 2.0
        deleted = False

        if header_visible:
            bg_value = (selected_bg_value if is_selected else row_bg_value) \
                + (hover_bg_boost if row_hovered else 0.0)
            if is_selected:
                add_shadow((row_rect[0], row_rect[1], row_right - row_left, row_height),
                           corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(row_rect[0], row_rect[1], row_rect[2], row_rect[3],
                                      _u32(_mix(tint, bg_value, factor, saturation)),
                                      rounding=corner)

            # chevron (expand/collapse) + play chips
            chevron_clicked, chip_edge = _button(
                row_left + px(4), chip_y, chip_width, px(22),
                chevron_down_icon if is_expanded else chevron_right_icon,
                tint, icon_only=True)
            if chevron_clicked:
                if is_expanded:
                    expanded_map.pop(key, None)
                else:
                    expanded_map[key] = True
                is_expanded = not is_expanded
                request_render()
            play_clicked, chip_edge = _button(chip_edge + px(2), chip_y, chip_width, px(22),
                                              play_icon, tint, icon_only=True)
            if play_clicked and not is_recording and not engine_busy:
                draw_state.selected_key = key
                Orchestrator.play(orchestration)
                changed = True

            # right side, outermost in: delete · restore · rename · tag
            delete_left = row_right - px(4) - chip_width
            if row_hovered and not is_take and not is_playing and not renaming:
                delete_clicked, _ = _button(delete_left, chip_y, chip_width, px(22),
                                            delete_icon, tint, icon_only=True)
                if delete_clicked:
                    orchestrations.pop(key, None)
                    expanded_map.pop(key, None)
                    tab_map.pop(key, None)
                    changed = True
                    deleted = True
            restore_left = delete_left - button_gap - chip_width
            restore_tint = restore_on_tint if orchestration.restore_on_finish else tint
            restore_clicked, _ = _button(restore_left, chip_y, chip_width, px(22),
                                         restore_icon, restore_tint,
                                         active=orchestration.restore_on_finish,
                                         icon_only=True)
            if restore_clicked:
                orchestration.restore_on_finish = not orchestration.restore_on_finish
                changed = True
            rename_left = restore_left - button_gap - chip_width
            if row_hovered and not renaming:
                rename_clicked, _ = _button(rename_left, chip_y, chip_width, px(22),
                                            rename_icon, tint, icon_only=True)
                if rename_clicked:
                    _start_rename(key, orchestration)
                    renaming = True

            if is_playing:
                tag = f"{Orchestrator._replay_index}/{len(orchestration.events)}"
            else:
                tag = f"{orchestration.duration:g}s · {len(orchestration.events)} ev"
            tag_width = imgui.calc_text_size(tag)[0]
            tag_left = rename_left - button_gap - tag_width
            draw_list.add_text(tag_left,
                               row_y + (row_height - imgui.get_text_line_height()) / 2.0
                               + text_nudge_y, _u32(_mix(tint, dim_text_value, factor,
                                                         text_saturation)), tag)

            # name: inline rename input, or the label (double-click to rename)
            name_left = chip_edge + px(8)
            if deleted:
                pass
            elif renaming:
                imgui.set_cursor_screen_pos((name_left, row_y + px(3)))
                imgui.push_item_width(max(px(90), min(px(240),
                                                      tag_left - name_left - px(10))))
                if panel_state is not None and getattr(panel_state, "_rename_focus", False):
                    imgui.set_keyboard_focus_here()
                    panel_state._rename_focus = False
                entered, text = imgui.input_text(f"##orch-rename-{key}",
                                                 draw_state.rename_text or "", 128,
                                                 imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
                imgui.pop_item_width()
                draw_state.rename_text = text
                item_active = imgui.is_item_active()
                was_active = (getattr(panel_state, "_rename_was_active", False)
                              if panel_state is not None else True)
                if entered or (was_active and not item_active):
                    changed = _commit_rename(orchestration) or changed
                elif panel_state is not None:
                    panel_state._rename_was_active = item_active
            else:
                text_value = row_text_value + (hover_text_boost if row_hovered else 0.0)
                name_color = _mix((record_tint if is_take else tint), text_value,
                                  factor, text_saturation)
                label = orchestration.name + ("  (recording)" if is_take else "")
                draw_list.add_text(name_left,
                                   row_y + (row_height - imgui.get_text_line_height()) / 2.0
                                   + text_nudge_y, _u32(name_color), label)
                if dbl is not None and _in((name_left, row_y, tag_left, row_y + row_height),
                                           dbl[0], dbl[1]):
                    _start_rename(key, orchestration)

            # row click (outside the chips): select
            if not deleted and not renaming and click is not None \
                    and _in(row_rect, click[0], click[1]) \
                    and click[0] < rename_left - button_gap:
                if selected_key != key:
                    draw_state.selected_key = key
                    changed = True
        row_y += row_stride
        if deleted:
            continue

        # ---- expanded detail: Commands (generalized) | Events (raw) ----
        if is_expanded:
            current_tab = tab_map.get(key, "commands")
            tab_x = row_left + detail_indent
            for tab_key, tab_label in (("commands", "Commands"), ("events", "Events")):
                width = imgui.calc_text_size(tab_label)[0] + 2 * tab_pad_x
                tab_clicked, tab_edge = _button(tab_x, row_y, width, tab_height,
                                                tab_label, tint,
                                                active=current_tab == tab_key)
                if tab_clicked and current_tab != tab_key:
                    tab_map[key] = tab_key
                    current_tab = tab_key
                    request_render()
                tab_x = tab_edge + button_gap
            row_y += tab_height + px(4)
            if current_tab == "events":
                detail = group_events(orchestration.events, orchestration.cues)
                if not detail:
                    detail = [(0, 0, "", "no events yet — press Record", "")]
                _detail_rows(detail, is_playing)
            else:
                detail = generalized_commands(orchestration)
                if not detail:
                    detail = [(0, 0, "", "no commands yet — effects appear "
                                         "as recording touches the undo stacks", "")]
                _detail_rows(detail, is_playing)
            row_y += px(4)

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
