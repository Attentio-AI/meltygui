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
shows a matching change; a missing cue first runs the cue target's
PRECONDITIONS (the same list the window shows beside the command — closed
/ collapsed / scrolled out / covered, satisfied by change_value's solver
at the recorded press point) and replays the gesture, for ANY cue with a
target regardless of kind or stack; a cue without a target gets the old
tile re-aim (edits) or none (effects); a second miss aborts with a notice
naming what was tried. With an orchestration's restore checkbox on, finishing a
replay walks both undo stacks back to where they stood at replay start —
the undo stack IS the restore mechanism.

The window is fast_dock-style: raw draw-list rows, manual hit-testing,
clicks resolved from left_mouse_down while hovered; orchestrator_sync()
(called once per frame from the always-rendering root) polls cue capture
and repaints the cached tile when engine state changes.
"""
import collections
import time
import types

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

# Remap value meaning "no override": open the target's gates, then play the
# recorded gesture verbatim.
_KEEP = object()


def _stacks():
    """The undo timelines cues are cut from and restore unwinds — name ->
    UndoStack. Restore runs in this dict's ORDER: edits first (their undo
    requests need the views still open), navigation after. (The effects
    LEDGER below is the third cue source — observable but not undoable, so
    it is deliberately not in this dict: restore never unwinds it.)"""
    return {"edits": UndoManager.stack, "navigation": NavUndo.stack}


class EffectLedger:
    """Observable-but-not-undoable effects — the third cue source beside the
    two undo stacks. Framework points publish one line through
    Melty.effect_hook when something REAL happened that no stack records:
    an actual window raise (apply_move_to_front — what makes a fast-dock
    row click verifiable with zero dock structure: the click is identified
    by its effect, "Voxels came to front", not by which pixel was hit), a
    fired flat_button (headers.flat_button — view_id + rect, so button cues
    carry the same press-fraction geometry as leaf-editor cues).

    `seq` plays the role group_id plays for the stacks: recording sweeps
    entries past its mark into cues (stack="effects"), replay verification
    scans entries past the replay mark. Bounded ring; class attrs so
    hotswap keeps live state."""

    entries = collections.deque(maxlen=256)
    next_seq = 0

    @classmethod
    def note(cls, kind, name, ds=None, rect=None):
        cls.next_seq += 1
        cls.entries.append(types.SimpleNamespace(
            seq=cls.next_seq, kind=str(kind), name=str(name),
            draw_state=ds, rect=rect, frame=Melty.frame_count))


def _wrap_text(text, width):
    """Greedy word wrap for the draw list (imgui measures, we break): words
    longer than the width break mid-word so nothing overflows."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else f"{line} {word}"
            if imgui.calc_text_size(candidate)[0] <= width or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
            while imgui.calc_text_size(line)[0] > width and len(line) > 1:
                cut = len(line)
                while cut > 1 and imgui.calc_text_size(line[:cut])[0] > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line)
    return lines or [""]


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


def _last_press_abs(take):
    """The take's most recent mouse press in ABSOLUTE coordinates —
    whether it is still absolute or already claimed by an earlier cue
    (window-relative, trailing cue index): un-anchor through that cue's
    recorded origin. None when the take holds no press."""
    for event in reversed(take.events):
        if event[1] != "down":
            continue
        x, y = event[3], event[4]
        if len(event) > 5:
            cue_index = event[5]
            anchor = (cue_get(take.cues[cue_index], "anchor")
                      if 0 <= cue_index < len(take.cues) else None)
            if anchor is None:
                return None
            x, y = x + anchor[2], y + anchor[3]
        return (x, y)
    return None


def _press_abs_at(take, index):
    """Absolute (x, y) of the press event at `index` (un-anchored through
    the cue it belongs to when relativized), or None."""
    event = take.events[index]
    if event[1] != "down":
        return None
    x, y = event[3], event[4]
    if len(event) > 5:
        cue_index = event[5]
        anchor = (cue_get(take.cues[cue_index], "anchor")
                  if 0 <= cue_index < len(take.cues) else None)
        if anchor is None:
            return None
        x, y = x + anchor[2], y + anchor[3]
    return (x, y)


def _group_press(take, since, rect):
    """(index, (x, y)) of the press that starts an edit cue's gesture: the
    FIRST mouse press at/after `since` (the previous edit cue's claim) that
    lands inside the target's `rect` (left, top, width, height). A colour
    edit is chip click → popover drag with the cue cut mid-drag: the chip
    click is the press on the target, the popover drag is outside it; a
    text edit is focus click → keystrokes; a drag is its own press. A
    stray click elsewhere (raising the window first) is outside the rect
    and skipped. Falls back to the LAST press when none is inside."""
    left, top, width, height = rect
    last = None
    for index in range(len(take.events)):
        point = _press_abs_at(take, index) if take.events[index][1] == "down" \
            and take.events[index][2] in _IMGUI_BUTTON else None
        if point is None:
            continue
        last = (index, point)
        if index < since:
            continue
        if width > 0 and height > 0 and left <= point[0] <= left + width \
                and top <= point[1] <= top + height:
            return index, point
    return last if last is not None else (None, None)


def make_cue(change, stack_name, event_index, take, press_window=None, since=0):
    """The enriched cue: effect signature (kind/name/old/new/direction),
    addressing (chain — maximal capture, minimal_path trims late; tile),
    geometry (anchor window + the target leaf's rect and the press point as
    a FRACTION of it — what re-targets a take onto a sibling field), and the
    applicability signature (editor + value_type — what licenses reusing
    this take on a different field of the same widget kind)."""
    from src.lsd.gl_gui.view.playground.selectors import name_chain
    ds = getattr(change, "draw_state", None)
    anchored = _cue_anchor_window(change, press_window=press_window)
    anchor = _cue_anchor(change, press_window=press_window)
    leaf_rect = None
    press_frac = None
    press_index = None
    if ds is not None:
        left = getattr(ds, "abs_left", None)
        top = getattr(ds, "abs_top", None)
        width = getattr(ds, "width", 0) or 0
        height = getattr(ds, "height", 0) or 0
        if anchored is not None and anchored[0] is ds:
            # the target IS the anchor window: its rect is read where the
            # window sat when the press landed (the anchor origin), not
            # where the gesture left it - a colour cue's press is on the
            # window, and measured against the anchor rect it read as the
            # bottom edge / a corner (clamped fraction, 09-01)
            left, top = anchored[1], anchored[2]
        if left is not None and top is not None and anchor is not None:
            leaf_rect = (float(left - anchor[2]), float(top - anchor[3]),
                         float(width), float(height))
        # the press that STARTED this cue's gesture and the fraction
        # (group_press); a keyboard-only edit has no press and the
        # executors default to the leaf's center
        press_index, press = (_group_press(take, since, (left or 0, top or 0, width, height))
                              if left is not None else (None, None))
        if press is not None and left is not None and width > 0 and height > 0:
            press_frac = (max(0.0, min(1.0, (press[0] - left) / width)),
                          max(0.0, min(1.0, (press[1] - top) / height)))
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
        "press_index": press_index,       # the press that starts the gesture (replay retargets from it)
        "tile": repr(getattr(ds, "_tile_id", None))[:200],
    }


def _front_window_name():
    """Display name of the frontmost OPEN registered window (registration
    order is z-order, front = last). The state side of raise verification."""
    windows = getattr(Melty, "registered_windows", None) or {}
    for managed in reversed(list(windows.values())):
        ds = getattr(managed, "draw_state", None)
        if ds is not None and not getattr(ds, "closed", False):
            return str(getattr(managed, "name", "")).split("##")[0]
    return None


def _contains(ds, x, y):
    left = getattr(ds, "abs_left", None)
    top = getattr(ds, "abs_top", None)
    if left is None or top is None:
        return False
    return (left <= x <= left + (getattr(ds, "width", 0) or 0)
            and top <= y <= top + (getattr(ds, "height", 0) or 0))


def _on_header_band(ds, x, y):
    """(x, y) inside ds's header strip (a window's title row)."""
    header = getattr(ds, "header_height", 0) or 0
    top = getattr(ds, "abs_top", None)
    return (header > 0 and top is not None and _contains(ds, x, y)
            and y < top + header)


def _body_press_raise(window, press, home=None):
    """True when the press raised `window` as a side effect of hitting its
    CONTENT: the press's home (`home`, the top-level view under it AT
    PRESS TIME — a summoned window covers the point afterwards, so cue-time
    geometry can't tell) is the window itself or a view nested in it, and
    the point sits on no header band (the window's own, or a nested
    window's — every hit-boxed view is checked). A press on a header, or
    whose home is elsewhere (dock row, another window's control), is an
    explicit raise. No home known → the window's geometry decides; no
    geometry → explicit."""
    if press is None or window is None:
        return False
    x, y = press
    if home is not None:
        node, steps = home, 0
        while node is not None and node is not window and steps < 64:
            parent = getattr(node, "parent_window", None)
            if parent is node:
                break
            node, steps = parent, steps + 1
        if node is not window:
            return False                  # press-time home elsewhere
    elif not _contains(window, x, y):
        return False
    if _on_header_band(window, x, y):
        return False
    for ds in (getattr(Melty, "_bvh_id_to_ds", None) or {}).values():
        if ds is not None and _on_header_band(ds, x, y):
            return False
    return True


def _window_under(x, y):
    """The click's HOME root: the topmost hit-boxed view under (x, y) — the
    BVH holds EVERY live interactive view, which matters because the Fast
    Dock is NOT a registered window (it draws straight from the root loop),
    so a registered-windows-only scan missed it and dock clicks got no
    anchor (absolute replay coordinates — the original-spot bug) — walked up
    its parent_window chain to the top-level view whose position the take
    should be relative to. Registered windows (reversed = front first) are
    the fallback when the BVH is empty (headless)."""
    best = None
    best_z = None
    for ds in (getattr(Melty, "_bvh_id_to_ds", None) or {}).values():
        if ds is None or not _contains(ds, x, y):
            continue
        z = getattr(ds, "z_pos", None) or getattr(ds, "abs_layer", 0) or 0
        if best_z is None or z >= best_z:
            best, best_z = ds, z
    if best is not None:
        node, steps = best, 0
        while steps < 64:
            parent = getattr(node, "parent_window", None)
            if parent is None or parent is node:
                break
            node, steps = parent, steps + 1
        return node
    windows = getattr(Melty, "registered_windows", None) or {}
    for managed in reversed(list(windows.values())):
        ds = getattr(managed, "draw_state", None)
        if ds is not None and not getattr(ds, "closed", False) and _contains(ds, x, y):
            return ds
    return None


def make_effect_cue(entry, take, press_window=None):
    """Cue from an effect-ledger entry — same dict shape as make_cue so the
    claiming / relativization / verification machinery serves it unchanged.
    Anchor = the window under the recorded press AT PRESS TIME
    (`press_window`, stashed by the tap when the down was recorded): by
    cue-cut time the effect has already happened — the window the dock
    summoned sits raised and topmost OVER the dock row, so a
    resolve-at-cue-time anchored to the summoned window, not the dock.
    leaf_rect / press_frac from the published control rect (flat_button)."""
    from src.lsd.gl_gui.view.playground.selectors import name_chain
    press = _last_press_abs(take)
    window_ds = press_window if press_window is not None else (
        _window_under(press[0], press[1]) if press is not None else None)
    if press is None:
        window_ds = None                     # a stash pairs with ITS effect only
    if window_ds is None and entry.rect is None and entry.draw_state is not None:
        window_ds = getattr(entry.draw_state, "parent_window", None)
    anchor = None
    if window_ds is not None:
        left = getattr(window_ds, "abs_left", None)
        top = getattr(window_ds, "abs_top", None)
        if left is not None and top is not None:
            anchor = (repr(getattr(window_ds, "_tile_id", None))[:200],
                      str(getattr(window_ds, "name", "?")).split("##")[0],
                      float(left), float(top))
    leaf_rect = None
    press_frac = None
    press_offset = None
    ds_for_offset = entry.draw_state
    if press is not None and ds_for_offset is not None:
        ds_left = getattr(ds_for_offset, "abs_left", None)
        ds_top = getattr(ds_for_offset, "abs_top", None)
        if ds_left is not None and ds_top is not None:
            # where the press sat relative to the affected view's top-left:
            # an expand arrow is a constant offset from its collection's
            # corner whatever the collection, so this generalizes an
            # expand demonstration to any collection gate
            press_offset = (float(press[0] - ds_left), float(press[1] - ds_top))
    if entry.rect is not None:
        rect_x, rect_y, rect_w, rect_h = entry.rect
        if anchor is not None:
            leaf_rect = (float(rect_x - anchor[2]), float(rect_y - anchor[3]),
                         float(rect_w), float(rect_h))
        if press is not None and rect_w > 0 and rect_h > 0:
            press_frac = (max(0.0, min(1.0, (press[0] - rect_x) / rect_w)),
                          max(0.0, min(1.0, (press[1] - rect_y) / rect_h)))
    return {
        "at": len(take.events),
        "stack": "effects",
        "kind": entry.kind,
        "name": entry.name,
        "chain": list(name_chain(entry.draw_state)) if entry.draw_state is not None else [],
        "old": None, "new": None, "new_repr": "", "direction": None,
        # the view the effect happened on: what makes a demonstration
        # transferable to subjects of the SAME kind (flat_value.effectable)
        "editor": getattr(getattr(entry.draw_state, "_view_func", None), "__name__", None),
        "value_type": None,
        "anchor": anchor,
        "leaf_rect": leaf_rect,
        "press_frac": press_frac,
        "press_offset": press_offset,
        "tile": repr(getattr(entry.draw_state, "_tile_id", None))[:200],
    }


def _cue_anchor(change, press_window=None):
    """The window a cue's events reference: (window_tile_repr, window_name,
    abs_left, abs_top), or None (those events stay absolute).

    - value Change: the field's PARENT WINDOW (the press was inside it).
    - WindowChange: the window under the press AT PRESS TIME when known
      (`press_window`, the tap's stash) — a dock row click toggles ANOTHER
      window, and anchoring on the toggled window re-referenced the dock
      click to wherever that window sits; the toggled window itself is only
      the fallback (the titlebar-✕ case, where they coincide).
    - WindowMoveChange: the moved window itself, at its PRE-drag position —
      the drag really is on that window, and its events happened before the
      move landed."""
    resolved = _cue_anchor_window(change, press_window=press_window)
    if resolved is None:
        return None
    window_ds, left, top = resolved
    return (repr(getattr(window_ds, "_tile_id", None))[:200],
            str(getattr(window_ds, "name", "?")), float(left), float(top))


def _cue_anchor_window(change, press_window=None):
    """(window_ds, left, top) behind _cue_anchor: the anchor window and its
    origin AT PRESS TIME — a moved window's is rewound by the change's
    delta, since the cue is cut when the drag has landed but its events
    (and the press's geometry) happened before. None when there is no
    window to anchor on."""
    ds = getattr(change, "draw_state", None)
    if ds is None:
        return None
    if isinstance(change, WindowMoveChange):
        window_ds = ds
    elif isinstance(change, WindowChange):
        window_ds = press_window if press_window is not None else ds
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
    return window_ds, float(left), float(top)


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
# Cue kinds recorded off the EDIT stack: a value the user set on some
# target. Every one of them is a command with a target and a press -
# whether or not an archetype can DRIVE its value (that is what
# _COMMAND_VERBS decides): the conditions and the gates-only replay
# (open / uncover, then the tape presses) apply to all of them.
_EDIT_KINDS = ("Change", "SetterChange")


def cue_gesture(cue):
    """Which press a cue's gesture is: "header" for a window move / raise
    (the header strip), "leaf" for everything else (the target's rect)."""
    return "header" if cue_get(cue, "kind") in ("WindowMoveChange", "raise") else "leaf"


def cue_press_frac(cue):
    """The demonstrated press as a fraction of the target's rect — the
    cue's own press_frac, else derived from press_offset + leaf_rect."""
    frac = cue_get(cue, "press_frac")
    if frac:
        return (float(frac[0]), float(frac[1]))
    offset, rect = cue_get(cue, "press_offset"), cue_get(cue, "leaf_rect")
    if offset and rect and rect[2] > 0 and rect[3] > 0:
        return (float(offset[0]) / float(rect[2]), float(offset[1]) / float(rect[3]))
    return None


def cue_press_offset(cue):
    """Where the recording pressed, as an OFFSET (px) from the cue TARGET's
    top-left — the cue's own press_offset (an effect cue: the press
    relative to the view the effect happened on), else the press fraction
    of its leaf_rect (an edit cue: the leaf IS the target). None without
    geometry (a legacy cue)."""
    offset = cue_get(cue, "press_offset")
    if offset:
        return (float(offset[0]), float(offset[1]))
    rect, frac = cue_get(cue, "leaf_rect"), cue_press_frac(cue)
    if rect and frac is not None:
        return (frac[0] * float(rect[2]), frac[1] * float(rect[3]))
    return None


def cue_control(cue):
    """The CONTROL the cue pressed, as (width, height, frac_x, frac_y) —
    its leaf_rect's size and the press's fraction of it. This bounds a
    precondition re-pick: a header button is the button, not the view it
    belongs to; a leaf editor is the leaf. None without geometry."""
    rect, frac = cue_get(cue, "leaf_rect"), cue_press_frac(cue)
    if not rect or frac is None:
        return None
    width, height = float(rect[2]), float(rect[3])
    if width <= 0 or height <= 0:
        return None
    return (width, height, float(frac[0]), float(frac[1]))


def cue_has_target(cue):
    """A cue whose gesture pressed a resolvable target — what the window
    lists preconditions for and the replay makes hittable first."""
    return bool(cue_get(cue, "chain")) and cue_get(cue, "kind") not in ("WindowChange",)


def move_delta(cue):
    """A WindowMoveChange cue's (dx, dy): its old/new window positions are
    tuples, stored as reprs — parsed back here. None when unparseable (a
    legacy cue): the tape then plays the drag verbatim."""
    import ast
    try:
        old = ast.literal_eval(str(cue_get(cue, "old")))
        new = ast.literal_eval(str(cue_get(cue, "new")))
        return (float(new[0]) - float(old[0]), float(new[1]) - float(old[1]))
    except (ValueError, SyntaxError, TypeError, IndexError):
        return None


def _format_value(value, value_type=None):
    """A cue value for a label: floats short, strings quoted — unless the
    cue says the value is NOT a str (`value_type`): a tuple / enum arrives
    as its repr string and is shown as written, never quoted."""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return value if value_type not in (None, "str") else repr(value)
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
        arg = None
        if kind in _EDIT_KINDS:
            verb = _COMMAND_VERBS.get(cue_get(cue, "editor"))
            if verb is not None:
                # editable command: the label stops at the first argument -
                # the window renders the value as an edit input after it
                # (recorded value pre-filled, override editable), a plain
                # consumer appends _format_value(arg) + ")".
                label = f'change_value("{path}",'
                arg = new
            elif cue_get(cue, "value_type") == "bool":
                verb = "expand"
                label = (f'expand "{path}"' if new in (True, "True")
                         else f'collapse "{path}"')
            else:
                verb = "set"
                label = f"set {path} = {_format_value(new, cue_get(cue, 'value_type'))}"
        elif kind == "WindowMoveChange":
            delta = move_delta(cue)
            verb = "move"                        # display_name is "move <window>"
            label = (f'{name} by ({delta[0]:.0f}, {delta[1]:.0f})' if delta is not None
                     else name)
        elif kind == "WindowChange":
            verb, label = "window", name         # "open <w>" or "close <w>"
        elif kind == "raise":
            verb, label = "raise", f'raise "{name}"'
        elif kind == "scroll":
            verb, label = "scroll", f'scroll "{name}"'
        elif kind == "button":
            verb, label = "button", f'press "{name}"'
        elif kind in ("expand", "collapse"):
            verb, label = "expand", f'{kind} "{path}"'
        else:
            verb, label = "goto", name
        rows.append((previous, at, verb, label, tag, arg))
        previous = at
    return rows


def auto_take_name(take):
    """A name from the take's own effects: the first leaf-edit cue as
    "field → value" (falling back to the first cue's display name), plus a
    "+N" for the remaining cues. None when there is nothing to name from."""
    cues = getattr(take, "cues", None) or []
    if not cues:
        return None
    main = next((cue for cue in cues
                 if cue_get(cue, "kind") in _EDIT_KINDS
                 and cue_get(cue, "editor") in _COMMAND_VERBS), cues[0])
    if cue_get(main, "kind") in _EDIT_KINDS and cue_get(main, "editor") in _COMMAND_VERBS:
        value = cue_get(main, "new", cue_get(main, "new_repr"))
        label = f"{cue_get(main, 'name')} → {_format_value(value)}"
    elif cue_get(main, "kind") in ("expand", "collapse", "raise", "button"):
        label = f"{cue_get(main, 'kind')} {cue_get(main, 'name')}"
    elif cue_get(main, "kind") in _EDIT_KINDS:
        value = cue_get(main, "new", cue_get(main, "new_repr"))
        label = f"{cue_get(main, 'name')} → {_format_value(value, cue_get(main, 'value_type'))}"
    else:
        label = str(cue_get(main, "name") or "Orchestration")
    extra = len(cues) - 1
    return f"{label}  +{extra}" if extra > 0 else label


def parse_argument(text, recorded):
    """Parse an argument edit box's text by the RECORDED value's type —
    float/int/bool coerce (bool accepts true/false/1/0), str passes raw.
    Returns (ok, value); a failed coercion keeps the previous value."""
    text = text.strip() if isinstance(text, str) else text
    try:
        if isinstance(recorded, bool):
            if str(text).lower() in ("true", "1", "yes", "on"):
                return True, True
            if str(text).lower() in ("false", "0", "no", "off"):
                return True, False
            return False, recorded
        if isinstance(recorded, int):
            return True, int(float(text))
        if isinstance(recorded, float):
            return True, float(text)
        return True, str(text)
    except (TypeError, ValueError):
        return False, recorded


def failure_report(failure):
    """Everything needed to debug a failed run, as text (the error bar's
    copy target): the failure line, the take's commands (with overrides),
    the grouped event list — the failed row marked — then the raw cues and
    raw events, engine state and the tails of the undo stacks / ledger."""
    take = failure.orchestration
    name = str(getattr(take, "name", None) or "task")
    where = ""
    if failure.cue_index is not None:
        where = f" (command {failure.cue_index + 1})"
    elif failure.event_index is not None:
        where = f" (event {failure.event_index})"
    lines = [f"ORCHESTRATION FAILURE: {name}{where}",
             f"reason: {failure.reason}",
             f"event_index={failure.event_index} cue_index={failure.cue_index}",
             f"status={Orchestrator.status!r} replay_speed="
             f"{Toggles.Orchestrator.replay_speed} cue_wait_frames="
             f"{Toggles.Orchestrator.cue_wait_frames}", ""]
    if take is None:
        return "\n".join(lines)
    events = getattr(take, "events", None) or []
    cues = getattr(take, "cues", None) or []
    overrides = getattr(take, "overrides", None) or {}
    fail_at = None
    if failure.cue_index is not None and failure.cue_index < len(cues):
        fail_at = cue_get(cues[failure.cue_index], "at", 0)
    elif failure.event_index is not None:
        fail_at = failure.event_index
    lines.append(f"take: {len(events)} events, {len(cues)} cues, "
                 f"duration {getattr(take, 'duration', 0)}s, "
                 f"restore={getattr(take, 'restore_on_finish', False)}, "
                 f"overrides={overrides}")
    lines += ["", "COMMANDS:"]
    for ordinal, row in enumerate(generalized_commands(take)):
        start, end, kind, label, tag, arg = (row + (None,))[:6]
        if arg is not None:
            value = overrides.get(str(ordinal), arg)
            label = f"{label} {_format_value(value)})" + \
                ("   [override]" if str(ordinal) in overrides else "")
        mark = " <<< FAILED" if ordinal == failure.cue_index else ""
        lines.append(f"  [{ordinal}] {kind:<8} {label}   ({start}-{end}) {tag}{mark}")
    if failure.cue_index is not None and failure.cue_index < len(cues):
        failed_cue = cues[failure.cue_index]
        if cue_has_target(failed_cue):
            from src.lsd.gl_gui.view.playground.change_value import describe_target, list_preconditions
            path = tuple(cue_get(failed_cue, "chain") or [cue_get(failed_cue, "name") or "?"])
            gesture, frac = cue_gesture(failed_cue), cue_press_frac(failed_cue)
            lines += ["", "TARGET (at report time — what the solver sees for the failed command):"]
            try:
                for field, value in describe_target(path, gesture=gesture, press_frac=frac).items():
                    lines.append(f"  {field}: {value!r}")
                for row in list_preconditions(path, gesture=gesture, press_frac=frac):
                    lines.append(f"  precondition: {row['kind']} '{row['node']}' — {row['label']}"
                                 f"   fixes: {[label for _c, label in row['fixes']]}")
            except Exception as error:
                lines.append(f"  (could not describe: {type(error).__name__}: {error})")
    lines += ["", "EVENTS (grouped):"]
    for start, end, kind, label, tag in group_events(events, cues):
        if kind == "cue":
            hit = fail_at is not None and start == fail_at
        else:
            hit = fail_at is not None and start <= fail_at < max(end, start + 1)
        lines.append(f"  {start:>4}-{end:<4} {kind:<7} {label}   {tag}"
                     + ("   <<< FAILED" if hit else ""))
    lines += ["", "CUES (raw):"]
    for index, cue in enumerate(cues):
        lines.append(f"  [{index}] {cue!r}")
    lines += ["", "RAW EVENTS:"]
    for index, event in enumerate(events):
        lines.append(f"  {index:>4} {event!r}"
                     + ("   <<< stopped here" if index == failure.event_index else ""))
    lines += ["", "UNDO STACKS (newest last, tail):"]
    for stack_name, stack in _stacks().items():
        tail = list(stack.history)[-12:]
        lines.append(f"  {stack_name}: {len(stack.history)} entries")
        for change in tail:
            lines.append(f"    gid={change.group_id} {type(change).__name__} "
                         f"'{change.display_name}' {_short_repr(change.old)} -> "
                         f"{_short_repr(change.new)}")
    lines.append(f"  effects: {len(EffectLedger.entries)} entries")
    for entry in list(EffectLedger.entries)[-12:]:
        lines.append(f"    seq={entry.seq} {entry.kind} '{entry.name}' frame={entry.frame}")
    # the screen's window order at report time - what the `behind` /
    # `obscured` preconditions read (back → front; `root` = a standalone
    # top-level window, the only kind that can be "the top window")
    lines += ["", "PAINT ORDER (back → front, at report time):"]
    for window in list(getattr(Melty, "paint_ordered_ds", None) or []):
        parent = getattr(window, "parent_window", None)
        lines.append(
            f"  {str(getattr(window, 'name', '?')).split('##')[0]:<28} "
            f"layer={getattr(window, 'layer', None)} "
            f"{'root' if parent is None or parent is window else 'nested'} "
            f"closable={getattr(window, 'closable', None)} "
            f"closed={getattr(window, 'closed', None)}/{getattr(window, 'abs_closed', None)} "
            f"rect=({getattr(window, 'abs_left', None)}, {getattr(window, 'abs_top', None)}, "
            f"{getattr(window, 'width', None)}, {getattr(window, 'height', None)})")
    lines += ["", "ENGINE TRACE (per pump, state at pump start — real_io_* is the PHYSICAL "
                  "mouse, `virtual` the engine's; injected = previous pump; target.blit_last = "
                  "its tile came from the blit cache on the frame just rendered):"]
    for row in list(Orchestrator._trace):
        row = dict(row)
        injected = row.pop("injected", [])
        target = row.pop("target", None)
        lines.append("  " + " ".join(f"{k}={v}" for k, v in row.items()))
        if target is not None:
            lines.append(f"      target: {target}")
        if injected:
            lines.append(f"      injected: {injected}")
    return "\n".join(lines)


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
    _record_marks = {}          # stack name -> group id at record start / last cue sweep
    _record_base_marks = {}     # stack name -> group id at record START (restore target;
                                # _record_marks advances with every cue sweep)
    _relativized_upto = 0       # events before this index are claimed by a cue's anchor
    _cue_changes = []           # [(cue index, Change)] cut this recording: their `new` tracks the coalescing edit
    _last_edit_claim = 0        # event index the previous EDIT cue's gesture ended at: the next gesture starts after
    _gesture_cue = None         # cue claiming the IN-FLIGHT gesture's remaining events
    _gesture_buttons = set()    # buttons still held from that gesture
    _gesture_end_pending = False
    _last_press_window = None   # window under the last recorded press, AT press time
    _last_press_xy = None       # that press's absolute coordinates

    _replay_t0 = 0.0
    _replay_index = 0           # next event to inject
    _replay_marks = {}          # stack name -> group id at replay start (restore + restore baseline)
    _cue_cursor = 0             # next cue (index into orch.cues) not yet armed
    _cue_pending = None         # armed cue awaiting verification
    _cue_wait = 0
    _cue_corrected = False
    _matched_ids = set()        # id(change) of live changes already claimed by a cue
    # Cue indices whose failed verification already ran the PRECONDITION
    # correction (the window's own list, satisfied then the gesture
    # replayed): a second miss of the same cue aborts instead of looping.
    _precondition_corrected = {}   # cue_index -> the task's attempts log
    _anchor_cache = {}          # cue_index -> (left, top): resolved anchor origins this replay
    _replay_end = None          # exclusive event bound of a single replay run (None means full)
    _replay_partial = False     # partial run: restore_on_finish is skipped
    # The most recent failure, shown by the window (red row highlight + the
    # header error bar): orchestration ref, reason, event_index (where the
    # replay stopped), cue_index (the cue that failed / the one that
    # aborted). Cleared when that take later finishes successfully, or by
    # the error bar's dismiss chip.
    last_failure = None
    # The most recent success (green row wash + "- ok" tag). A failure of
    # the same take displaces it, and vice versa.
    last_success = None
    _click_seq = 0              # unique keys for click-ripple emphasis notes
    # Press settling. The handler dispatches a press against the hover set
    # the PREVIOUS render registered, and that render showed the cursor where
    # stamp_io put it a frame earlier - so a press injected in the same
    # pump as the move lands wherever the cursor WAS (the real pointer is
    # the play button: it grabbed the orchestrator window and the servo's
    # moves dragged it across the screen). A press is only injected when
    # SETTLE_PUMPS pumps have passed since the last injected move (move →
    # stamp_io → render → hover → press dispatches), and a replay
    # whose first event is a press gets a move to that spot first.
    SETTLE_PUMPS = 2
    _pump_count = 0
    _last_move_pump = -10
    _cursor_settled = False     # a move has been injected since play/submit
    # Continuous mouse (Toggles.Orchestrator.continuous_mouse): an injected
    # move/press far from the virtual mouse is queued behind an eased glide
    # (one step per pump), later injections queue FIFO behind it - so a
    # reused click (a gate fragment, an offset click, a cue resync after
    # a remap) TRAVELS to its spot instead of snapping, which is also the
    # frames the hover / press-ready machinery needs. Items: ("glide", x, y)
    # or a raw event tuple.
    _glide_queue = collections.deque()
    PRESS_JUMP_PX = 24.0        # a press this far from the cursor glides there first, mouse up
    _deferred_press = None      # (input_id, x, y) - correction presses once settled
    _deferred_release = None    # the above's release, the pump AFTER its press
    # Value remaps: the take's overrides applied DURING replay. A gesture
    # whose cue value is overridden plays as the override instead of its
    # recorded drag - the same kind of remap anchoring is (positions follow
    # the window; here the drag length follows the value) - and the tape
    # resumes after that gesture's release. {down_index: (up_index,
    # cue_index, value)}; `_remap` is the running (task, generator, ...).
    _remaps = {}
    _remap = None
    _remap_universe = None
    # Retargeting a gates-only remap: the tape's gesture is injected where
    # its TARGET is now, not where it was recorded - ((dx, dy), first event
    # index, last event index) added to the events of that span by
    # _event_xy`. A resolved target (the cue's chain) beats the recorded
    # spot: reordering a list moved the item, and a verbatim press landed
    # on whatever now sat at its old index (Lukas 09-01).
    _remap_shift = None
    # Anchor inheritance: an event recorded with no anchor of its own (a
    # stretch no cue claimed - e.g. before an effect cue that had no press)
    # gets the anchor of the stretch BEFORE it (or, for a take's leading
    # events, the first anchored stretch after), so the cursor keeps ONE
    # window offset instead of jumping out to stale absolute coordinates
    # and back. {event_index: cue_index}, built by play().
    _inherited_anchor = {}
    _injecting_index = None
    # REAL mouse buttons currently held (tracked from the tap, always: the
    # play click's own press is in there when a task arms). Their releases
    # are ALWAYS delivered, and no virtual press is injected while one is
    # held: a physical click takes ~100 ms to release, the servo's click
    # landed inside that window, the muted real release left the real
    # press's drag session (the orchestrator window grabbed the pointer)
    # alive, and the servo's moves dragged it across the screen.
    _real_down = set()
    # Per-pump engine trace while recording (last 90 pumps) - dumped into the
    # failure reason as ENGINE TRACE so a copied report shows which link of
    # the injection chain (io stamp → imgui hover → melty hover → handler
    # dispatch → cue activation) broke.
    _trace = collections.deque(maxlen=600)
    _trace_injected = []        # events injected during the current pump
    _last_click = None          # (input_id, x, y) of the last injected press
    _injecting = False          # True while pump feeds the handler (tap lets those through)
    _restore_steps = []         # [(stack_name, mark_gid), ...] still to unwind
    _pending_release = set()    # buttons held at record start (the Record click)
    _task = None                # active ValueTask (change_value) — generator, stepped per frame
    # Preconditions per value command, as the window shows them: the body
    # names the (orchestration key, command ordinal) rows it has on screen
    # (`_precondition_watch`, rebuilt on every repaint of the window) and
    # orchestrator_sync re-lists them every precondition_refresh_frames
    # (`_preconditions`: key -> change_value.list_preconditions rows).
    _precondition_watch = set()
    _preconditions = {}
    _precondition_frame = -10 ** 9

    @classmethod
    def refresh_preconditions(cls, store):
        """Re-list the watched commands' preconditions (from orchestrator_sync).
        Returns True when any row changed."""
        from src.lsd.gl_gui.view.playground.change_value import list_preconditions
        changed = False
        fresh = {}
        for watch_key in list(cls._precondition_watch):
            orchestration_key, ordinal = watch_key
            orchestration = store.orchestrations.get(orchestration_key) if store else None
            cues = getattr(orchestration, "cues", None) or []
            if orchestration is None or not (0 <= ordinal < len(cues)):
                continue
            cue = cues[ordinal]
            if not cue_has_target(cue):
                continue
            path = tuple(cue_get(cue, "chain") or [cue_get(cue, "name") or "?"])
            gesture, frac = cue_gesture(cue), cue_press_frac(cue)
            try:
                rows = list_preconditions(path, gesture=gesture, press_frac=frac)
                if not rows:
                    # the "target hittable" row explains its verdict: the
                    # press point the solver will use and the window it
                    # finds in front there - a wrong leaf / point / front
                    # window is visible at a glance instead of a silent pass
                    from src.lsd.gl_gui.view.playground.change_value import describe_target
                    facts = describe_target(path, gesture=gesture, press_frac=frac)
                    rows = [{"key": ("hittable", facts.get("resolved") or "?"), "kind": "hittable",
                             "node": facts.get("resolved") or "?",
                             "label": f"target hittable — {facts.get('resolved')!s} "
                                      f"({facts.get('editor')}) at {facts.get('point')}, "
                                      f"front: {facts.get('front')!s}",
                             "fixes": []}]
            except Exception as error:               # a half-built tree mid-frame: show it, never raise
                rows = [{"key": ("error", str(error)[:60]), "kind": "error", "node": "",
                         "label": f"could not list: {type(error).__name__}: {error}"[:160],
                         "fixes": []}]
            fresh[watch_key] = rows
            previous = cls._preconditions.get(watch_key)
            if previous is None or [r["key"] for r in previous] != [r["key"] for r in rows] \
                    or [r["label"] for r in previous] != [r["label"] for r in rows]:
                changed = True
        if set(fresh) != set(cls._preconditions):
            changed = True
        cls._preconditions = fresh
        return changed

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
        real_button = args[0] if (kind in ("down", "up") and args
                                  and args[0] in _IMGUI_BUTTON) else None
        if real_button is not None:
            if kind == "down":
                cls._real_down.add(real_button)
            else:
                was_real = real_button in cls._real_down
                cls._real_down.discard(real_button)
                if was_real and not cls._real_down:
                    # Hover-driven tile invalidation is suppressed while a
                    # button is held (Melty.on_drag), so the settle clock
                    # restarts at a real release: only from here can the
                    # target's cursor leave the window and report hover.
                    cls._last_move_pump = cls._pump_count
        if cls.replaying is not None or cls._restore_steps or cls._task is not None:
            if kind == "key" and args and args[0] == glfw.KEY_ESCAPE:
                cls.abort("Esc")
            # A REAL release always reaches the handler - the play click's
            # own release lands after the take armed, and muting it leaves
            # its mouse capture alive under the pointer. (A release for a
            # button the real press put down is the backend's auto-release
            # guard reacting to the VIRTUALLY held button: that one stays
            # muted or it would cut its injected drag.)
            if kind == "up" and real_button is not None and was_real:
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
            event = (now, kind) + tuple(args)
            if kind == "down" and args and args[0] in _IMGUI_BUTTON:
                # The click's target window, right NOW - this frame's BVH
                # still holds the pre-click world, before the effect the
                # press triggers (window raise) restamps it. The effect cue's
                # anchor reads this stash, skipping a cue-time re-resolve.
                cls._last_press_window = _window_under(args[1], args[2])
                cls._last_press_xy = (args[1], args[2])
            if kind in ("move", "down", "up"):
                # In-flight gesture tail: a cue cut while a button was HELD
                # claims the REST of that gesture as it happens (see
                # poll_recording_into) - the release and its moves relativize
                # to the same anchor as the press, so a gesture is never
                # split across reference frames.
                event = cls._retag_gesture_event(take, event)
            if kind == "move" and take.events:
                last = take.events[-1]
                # jitter throttle - only as a move in the SAME frame
                # (absolute vs absolute, or relative with the same anchor)
                if (last[1] == "move" and len(last) == len(event)
                        and (len(event) == 4 or last[4] == event[4])
                        and abs(event[2] - last[2]) < Toggles.Orchestrator.move_sample_min_px
                        and abs(event[3] - last[3]) < Toggles.Orchestrator.move_sample_min_px):
                    return False              # sub-pixel jitter - skip
            take.events.append(event)
            if cls._gesture_end_pending:
                # The gesture's last button released: its span is fully
                # claimed - the NEXT movement starts a fresh (absolute)
                # stretch for the next cue to claim retroactively
                cls._gesture_end_pending = False
                cls._gesture_cue = None
                cls._relativized_upto = len(take.events)
        return False

    @classmethod
    def _retag_gesture_event(cls, take, event):
        """While a gesture tail is claimed (cue cut mid-hold): relativize the
        incoming mouse event against the claiming cue's anchor and track the
        held buttons; the release of the last one ends the claim."""
        if cls._gesture_cue is None or cls._gesture_cue >= len(take.cues):
            return event
        anchor = cue_get(take.cues[cls._gesture_cue], "anchor")
        if anchor is None:
            cls._gesture_cue = None
            return event
        anchor_x, anchor_y = anchor[2], anchor[3]
        if event[1] == "move":
            return (event[0], "move", event[2] - anchor_x, event[3] - anchor_y,
                    cls._gesture_cue)
        input_id = event[2]
        if event[1] == "down" and input_id in _IMGUI_BUTTON:
            cls._gesture_buttons.add(input_id)
        elif event[1] == "up":
            cls._gesture_buttons.discard(input_id)
            if not cls._gesture_buttons:
                cls._gesture_end_pending = True
        return (event[0], event[1], input_id, event[3] - anchor_x,
                event[4] - anchor_y, cls._gesture_cue)

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
        cls._record_marks["effects"] = EffectLedger.next_seq
        cls._record_base_marks = dict(cls._record_marks)
        cls._cue_changes = []
        cls._last_edit_claim = 0
        cls._relativized_upto = 0
        cls._gesture_cue = None
        cls._gesture_buttons = set()
        cls._gesture_end_pending = False
        cls._last_press_window = None
        cls.status = "recording"
        request_render()

    @classmethod
    def stop_recording(cls, via_hotkey=False):
        take = cls.recording
        cls.recording = None
        cls._pending_release = set()
        cls._gesture_cue = None
        cls._gesture_buttons = set()
        cls._gesture_end_pending = False
        cls._last_press_window = None
        cls.status = ""
        if take is None:
            return
        cls.poll_recording_into(take)         # cut cues one last time
        cls._cue_changes = []
        if via_hotkey:
            cls._trim_hotkey_tail(take)
        else:
            cls._trim_window_tail(take)
        # Leading "up" events are the release of the click that pressed
        # Record - they belong to the button gesture, not the take.
        while take.events and take.events[0][1] == "up":
            take.events.pop(0)
        take.duration = round(take.events[-1][0], 2) if take.events else 0.0
        # Auto-name from the take's events - a user-given name is never
        # overwritten (custom_name, set by the window's rename commit).
        if not getattr(take, "custom_name", False):
            generated = auto_take_name(take)
            if generated:
                take.name = generated
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
        # A cue is cut on the FIRST frame its Change exists: the gesture
        # goes on (a drag coalesces per frame, a command run per keystroke)
        # but the cue's value must be where the gesture ENDED - the endpoint
        # of the demonstration, what a replay drives to - not the first
        # tick (ranked cues read -87 for a drag that went further, 09-01).
        for cue_index, change in cls._cue_changes:
            if not (0 <= cue_index < len(take.cues)) or not isinstance(take.cues[cue_index], dict):
                continue
            cue = take.cues[cue_index]
            new = _primitive_or_repr(change.new)
            if new != cue.get("new"):
                cue["new"] = new
                cue["new_repr"] = _short_repr(change.new)
                cue["value_type"] = type(change.new).__name__
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
                take.cues.append(make_cue(change, name, len(take.events), take,
                                          press_window=cls._last_press_window,
                                          since=cls._last_edit_claim if name == "edits" else 0))
                if name == "edits":
                    cls._cue_changes.append((len(take.cues) - 1, change))
                    cls._last_edit_claim = len(take.events)
                # The cue's parent window re-references the mouse events that
                # LED to it: absolute → window-relative, so replay can follow
                # the window wherever it sits now. Events after the last cue
                # keep absolute coordinates - the fallback.
                cls._relativize_events(take, len(take.events), len(take.cues) - 1)
                cls._claim_open_gesture(take)
                mark = max(mark, gid)
            cls._record_marks[name] = max(cls._record_marks.get(name, 0), mark)
        # ---- effects ledger sweep (the third cue source) ----
        effects_mark = cls._record_marks.get("effects", 0)
        for entry in list(EffectLedger.entries):
            if entry.seq <= effects_mark:
                continue
            effects_mark = entry.seq
            # A raise is a cue only if the press was explicit - on a
            # header band, or outside the raised window (dock row, another
            # window's control). A body click's auto-raise is not one.
            if (entry.kind == "raise"
                    and Toggles.Orchestrator.raise_cue_needs_explicit_press
                    and _body_press_raise(entry.draw_state, cls._last_press_xy,
                                          cls._last_press_window)):
                continue
            # consecutive-duplicate entries: press-raise cascade (every click
            # inside a window re-raises it in some paths) folds to one cue
            if take.cues:
                last = take.cues[-1]
                if (cue_get(last, "stack") == "effects"
                        and cue_get(last, "kind") == entry.kind
                        and cue_get(last, "name") == entry.name):
                    continue
            take.cues.append(make_effect_cue(entry, take,
                                             press_window=cls._last_press_window))
            cls._relativize_events(take, len(take.events), len(take.cues) - 1)
            cls._claim_open_gesture(take)
        cls._record_marks["effects"] = max(cls._record_marks.get("effects", 0),
                                           effects_mark)

    @classmethod
    def _claim_open_gesture(cls, take):
        """A cue cut MID-GESTURE (a button still held — the effect fired on
        the press, e.g. WindowChange / a raise, or mid-drag) claims the rest
        of that gesture too: the release and its moves relativize to the
        same anchor as they arrive (tap → _retag_gesture_event), so a
        gesture is never split across reference frames and the next
        movement re-anchors fresh."""
        held = set()
        for event in take.events:
            if event[1] == "down" and event[2] in _IMGUI_BUTTON:
                held.add(event[2])
            elif event[1] == "up":
                held.discard(event[2])
        if held and cue_get(take.cues[-1], "anchor") is not None:
            cls._gesture_cue = len(take.cues) - 1
            cls._gesture_buttons = held
            cls._gesture_end_pending = False

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
    def play(cls, orchestration, start=0, end=None, universe=None, generalize=False):
        """Replay the take — or, with start/end, just ONE command's event
        span (the per-row run buttons): events [start, end) inject, cues
        with `at` in (start, end] arm and verify, cues beyond stay silent,
        and restore_on_finish is SKIPPED for a partial run (restoring after
        one action would undo it on the spot).

        Two modes, ONE code path for anything with a value (Lukas 09-01):
        `generalize=False` plays the recording — every gesture verbatim,
        its target's gates (collapsed parents, closed windows) opened
        first, overrides ignored; `generalize=True` runs every leaf edit
        through change_value, the override standing in for the recorded
        value where one is set and the RECORDED value otherwise — so an
        un-overridden command takes exactly the path an overridden one
        does, never a separate replay branch."""
        if cls.recording is not None or cls.replaying is not None or cls._restore_steps:
            return
        if not orchestration.events:
            notify("Orchestration is empty — record it first", tag="orchestrator")
            return
        start = max(0, min(start, len(orchestration.events)))
        # a new run clears the last outcome (red row / error bar / green row)
        # so what lands on screen is only THIS run's
        cls.last_failure = None
        cls.last_success = None
        cls.replaying = orchestration
        cls._replay_end = end
        cls._replay_partial = start > 0 or end is not None
        cls._remaps = cls._gesture_remaps(orchestration, generalize=generalize)
        cls._remap = None
        cls._remap_shift = None
        cls._glide_queue.clear()
        cls._inherited_anchor = cls._anchor_inheritance(orchestration)
        cls._remap_universe = universe        # headless tests: the fake tree; live: the cache
        # A PARTIAL run's timeline starts at its span's first event (no
        # lead-in wait); a full play keeps the recorded delay before the
        # first event - dt 0 stays dt 0.
        speed = max(0.05, Toggles.Orchestrator.replay_speed)
        span_t0 = (orchestration.events[start][0]
                   if 0 < start < len(orchestration.events) else 0.0)
        cls._replay_t0 = time.monotonic() - span_t0 / speed
        cls._replay_index = start
        # cues at indexes before the span start belong to earlier commands
        cls._cue_cursor = sum(1 for cue in orchestration.cues
                              if cue_get(cue, "at", 0) <= start)
        cls._cue_pending = None
        cls._cue_wait = 0
        cls._cue_corrected = False
        cls._matched_ids = set()
        cls._precondition_corrected = {}
        cls._last_click = None
        cls._replay_marks = {name: stack._next_group_id
                             for name, stack in _stacks().items()}
        cls._replay_marks["effects"] = EffectLedger.next_seq
        cls._anchor_cache = {}
        cls._cursor_settled = False
        cls._last_move_pump = -10
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
        """Per frame from SplitOverlayRenderer.process_inputs — BEFORE
        imgui.new_frame and stamp_io, where real input enters, so an injected
        event reaches the handler and imgui's io in the same frame (a real
        press's ordering; see the note there): inject every event whose
        recorded time has elapsed,
        pausing at cues; step a pending restore one undo group per frame
        (undo writes apply on the NEXT frame's render, so batching them in
        one frame would overwrite each other)."""
        cls._pump_count += 1
        if cls._glide_queue:
            cls._drain_glide()
        if cls.replaying is not None or cls._task is not None:
            cls._trace_snapshot()
            if cls._real_button_held():
                # A physical button is still down (the play click, most
                # often): its drag target - the window under the pointer -
                # is alive until it releases, and ANY injected move would
                # drive it (the orchestrator gets jumped by the virtual
                # mouse's then). Idle until the real release.
                request_render()
                return
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
        if cls._deferred_release is not None:
            input_id, x, y = cls._deferred_release
            cls._deferred_release = None
            cls._inject((0.0, "up", input_id, x, y))
            return
        if cls._deferred_press is not None:
            input_id, x, y = cls._deferred_press
            if cls.press_ready((x, y)):
                cls._deferred_press = None
                cls._inject((0.0, "down", input_id, x, y))
                # the release lands NEXT pump: imgui stamps io once a frame,
                # a press and release in one pump is no click
                cls._deferred_release = (input_id, x, y)
            return                           # the correction now owns this pump
        if cls._remap is not None:
            cls._step_remap()
            return
        if cls._cue_pending is not None and not cls._resolve_pending_cue():
            return
        speed = max(0.05, Toggles.Orchestrator.replay_speed)
        now = (time.monotonic() - cls._replay_t0) * speed
        events = orchestration.events
        cues = orchestration.cues
        end = cls._replay_end if cls._replay_end is not None else len(events)
        while cls._replay_index < end:
            if cls._replay_index in cls._remaps:
                # the gesture that set an overridden value: the servo plays
                # it (press → probe → drive to the new value → release)
                from src.lsd.gl_gui.view.playground.change_value import ValueTask
                up_index, cue_index, value = cls._remaps.pop(cls._replay_index)
                cue = cues[cue_index]
                path = tuple(cue_get(cue, "chain") or [cue_get(cue, "name") or "?"])
                task = ValueTask(path, None if value is _KEEP else value,
                                 universe=cls._remap_universe)
                task.gates_only = value is _KEEP     # open gates, where the tape presses
                task.gesture = cue_gesture(cue)      # "header": the move archetype, `to` = (dx, dy)
                task.press_frac = cue_press_frac(cue)
                task.orchestration = orchestration
                task.start_frame = Melty.frame_count
                # the press lands where the recording pressed ON THE TARGET -
                # its offset from the target's live top-left, so the item
                # may have moved (a glide, a scroll fix) and the click
                # follows it; a re-pick stays on the CONTROL that was
                # pressed. A legacy cue with no geometry presses right
                # where the tape recorded it (re-anchored).
                task.press_offset = cue_press_offset(cue)
                task.control = cue_control(cue)
                if task.press_offset is None and task.press_frac is None:
                    cls._injecting_index = cls._replay_index
                    task.press_point = cls._event_xy(events[cls._replay_index], 3)
                    cls._injecting_index = None
                # the recording's rhythm around the gesture: the pause before
                # the press (last approach move → down) and before the drag
                # starts (down → first move) - a drag that started the frame
                # the cursor arrived behaves differently from the recording
                down_index = cls._replay_index
                before_press = (events[down_index][0] - events[down_index - 1][0]
                                if down_index > 0 else 0.0)
                first_move = next((events[i][0] for i in range(down_index + 1, up_index + 1)
                                   if events[i][1] == "move"), events[down_index][0])
                task.pacing = {"before_press": max(0.0, before_press) / speed,
                               "before_drag": max(0.0, first_move - events[down_index][0]) / speed,
                               # the gesture's own duration: the servo paces its travel on it
                               "drag_duration": max(0.0, events[up_index][0] - first_move) / speed}
                cls._remap = (task, task.run(), up_index, cue_index)
                cls.status = (f"replaying {cls._replay_index}/{len(events)} · "
                              + ("checking gates" if task.gates_only else str(task)))
                return
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
            shift = cls._remap_shift
            if shift is not None and shift[1] <= cls._replay_index <= shift[2]:
                # a retargeted gesture: this TAPE event moves with the
                # target (synthesized approach moves never do - they are
                # already aimed at the live point)
                event = cls._shift_event(event, shift[0])
            cls._injecting_index = cls._replay_index
            if event[1] == "down" and not cls.press_ready(cls._event_xy(event, 3)):
                # settle down (a glide to the button, hover registered); the
                # tape's clock PARKS at this press meanwhile - a glide that
                # ran on the clock made every event of the gesture due at
                # once when the press finally fired: down + up in one pump
                # (one io stamp: imgui never saw the click, the popover
                # never opened) and the tape typed on top of it (09-01)
                cls._replay_t0 = time.monotonic() - event[0] / speed
                cls._injecting_index = None
                break                        # resumes next pump
            cls._inject(event, glide=False)
            cls._injecting_index = None
            cls._replay_index += 1
            if cls._remap_shift is not None and cls._replay_index > cls._remap_shift[2]:
                cls._remap_shift = None            # the retargeted gesture has played
            cls.status = f"replaying {cls._replay_index}/{len(events)}"
            if event[1] in ("down", "up") and event[2] in _IMGUI_BUTTON:
                # one button level per frame: imgui reads the level once a
                # frame, so a press and its release in the same pump is no
                # click at all - the rest of the tape waits a frame
                break
        if cls._replay_index >= end:
            # Trailing cues (the final click's undo change lands AFTER the
            # last event) still gate the finish - arm and resolve them here.
            # A bounded run arms only cues within the span (at <= end).
            while cls._cue_pending is None and cls._cue_cursor < len(cues) \
                    and cue_get(cues[cls._cue_cursor], "at", 0) <= end:
                cls._cue_pending = cues[cls._cue_cursor]
                cls._cue_cursor += 1
                cls._cue_wait = 0
                cls._cue_corrected = False
                if not cls._resolve_pending_cue():
                    return
            if cls._cue_pending is None:
                cls._finish()

    @classmethod
    def _anchor_inheritance(cls, orchestration):
        """{event_index: cue_index} for every mouse event that carries no
        anchor: the nearest anchored event before it, else the nearest after."""
        events = orchestration.events
        cues = orchestration.cues
        def anchored(event):
            slot = 4 if event[1] == "move" else 5
            if len(event) > slot and 0 <= event[slot] < len(cues) \
                    and cue_get(cues[event[slot]], "anchor") is not None:
                return event[slot]
            return None
        inherited = {}
        last = None
        pending = []
        for index, event in enumerate(events):
            if event[1] not in ("move", "down", "up"):
                continue
            own = anchored(event)
            if own is not None:
                last = own
                for waiting in pending:              # pending unanchored events: the first anchor after
                    inherited[waiting] = own
                pending = []
            elif last is not None:
                inherited[index] = last
            else:
                pending.append(index)
        return inherited

    @classmethod
    def _gesture_remaps(cls, orchestration, generalize=False):
        """For every cue WITH A TARGET (an edit, an effect — a button, a
        raise, a scroll — whatever its stack), the gesture that produced
        it, with the value that gesture must set — see `play` for the two
        modes. Its target's preconditions are checked and fixed BEFORE the
        press ever lands; the tape never clicks into a covered / clipped /
        collapsed target and reads the miss. Keyed
        by the press's event index; the value is (last event of the
        gesture, cue index, value). A DRAG's Change coalesces while the
        button is held, so its press is still in flight at the cue; a TEXT
        edit's focus click is complete before the first keystroke, so its
        gesture is the nearest completed click before the cue plus the
        whole keystroke run around it (the servo types the final value
        itself, and the tape must not type again on top of it, 09-01)."""
        overrides = getattr(orchestration, "overrides", None) or {}
        events = orchestration.events
        remaps = {}
        for cue_index, cue in enumerate(orchestration.cues):
            if cue_get(cue, "kind") == "WindowMoveChange":
                # a window move is recorded at gesture END (the release is
                # before the cue): the completed header drag before the cue
                # is the gesture, the servo re-drags by the recorded delta
                delta = move_delta(cue)
                span = cls._completed_gesture(events, min(cue_get(cue, "at", 0), len(events)))
                if delta is not None and span is not None:
                    down_index, end_index = span
                    remaps[down_index] = (end_index, cue_index, delta if generalize else _KEEP)
                continue
            at = min(cue_get(cue, "at", 0), len(events))
            if cue_get(cue, "kind") not in _EDIT_KINDS or cue_get(cue, "editor") not in _COMMAND_VERBS:
                # no archetype drives this cue's value (a colour chip, an
                # imgui widget without a servo, an EFFECT cue: a button, a
                # raise, a scroll): its gesture still gets its gates opened
                # and the press uncovered BEFORE the tape plays it verbatim
                # - preconditions still depend on the widget, and the press
                # is never made into a covered / clipped target first.
                # (A legacy cue without a chain has no target to resolve:
                # verbatim tape, as before.)
                if not cue_has_target(cue):
                    continue
                span = cls._cue_gesture(events, cue, at)
                if span is not None:
                    down_index, end_index = span
                    remaps[down_index] = (end_index, cue_index, _KEEP)
                continue
            if _COMMAND_VERBS.get(cue_get(cue, "editor")) == "type":
                span = cls._cue_gesture(events, cue, at)
                if span is not None:
                    down_index, end_index = span
                    if generalize:
                        value = overrides.get(str(cue_index), cue_get(cue, "new"))
                    else:
                        value = _KEEP
                    remaps[down_index] = (end_index, cue_index, value)
                continue
            # generalized: the override, else the value the recording set -
            # one path either way. Recording: KEEP, the recorded drag plays
            # verbatim, but the target's preconditions (enabled focus,
            # closed popover) are still covered and opened before its playback;
            # a take's gates are a property of the take
            if generalize:
                value = overrides.get(str(cue_index), cue_get(cue, "new"))
            else:
                value = _KEEP
            at = min(cue_get(cue, "at", 0), len(events))
            down_index = None
            for index in range(at - 1, -1, -1):
                event = events[index]
                if event[1] == "up" and event[2] in _IMGUI_BUTTON:
                    break                       # a completed gesture: no press pending
                if event[1] == "down" and event[2] in _IMGUI_BUTTON:
                    down_index = index
                    break
            if down_index is None:
                continue
            button = events[down_index][2]
            up_index = next((index for index in range(down_index + 1, len(events))
                             if events[index][1] == "up" and events[index][2] == button),
                            len(events) - 1)
            remaps[down_index] = (up_index, cue_index, value)
        return remaps

    @staticmethod
    def _keyboard_event(event):
        """A key / char event, or the press/release of a keyboard key (the
        handler feeds those as down/up with a "key_N" id)."""
        return event[1] in ("key", "char") or (
            event[1] in ("down", "up") and event[2] not in _IMGUI_BUTTON)

    @staticmethod
    def _shift_event(event, delta):
        """The event with its position offset by `delta` (a relativized
        event keeps its anchor index — the shift is in screen space and
        adds after the anchor resolves)."""
        dx, dy = delta
        if event[1] == "move":
            return (event[0], "move", event[2] + dx, event[3] + dy) + tuple(event[4:])
        if event[1] in ("down", "up"):
            return (event[0], event[1], event[2], event[3] + dx, event[4] + dy) + tuple(event[5:])
        return event

    @classmethod
    def _cue_gesture(cls, events, cue, at):
        """(press index, last event index) of an edit cue's whole gesture:
        from the press the recording named (`press_index` — the first press
        on the target, a chip click that opened a popover included) to the
        end of the interaction — the release of whatever is still held at
        the cue and every keystroke around it, stopping at the next fresh
        mouse press after the cue. Cues without a press_index (legacy)
        fall back to _pressed_gesture."""
        press_index = cue_get(cue, "press_index")
        if not (isinstance(press_index, int) and 0 <= press_index < len(events)
                and events[press_index][1] == "down"
                and events[press_index][2] in _IMGUI_BUTTON):
            return cls._pressed_gesture(events, at)
        return press_index, cls._gesture_end(events, at, press_index)

    @classmethod
    def _gesture_end(cls, events, at, down_index):
        """The last event of the gesture that starts at `down_index` and
        produced the cue at `at`: at least the release of that press; past
        the cue, keystrokes / moves / the release of a press still held
        continue it, a NEW mouse press after the cue ends it."""
        end = max(down_index, at - 1)
        held = set()
        for index in range(down_index, len(events)):
            event = events[index]
            if event[1] == "down" and event[2] in _IMGUI_BUTTON:
                if index > at - 1 and not held:
                    break                               # a fresh gesture after the cue
                held.add(event[2])
            elif event[1] == "up" and event[2] in _IMGUI_BUTTON:
                held.discard(event[2])
                end = max(end, index)
            elif cls._keyboard_event(event) and index >= at - 1:
                end = max(end, index)
            elif event[1] == "move" and index < at:
                end = max(end, index) if held else end
        return end

    @classmethod
    def _pressed_gesture(cls, events, at):
        """The gesture behind a cue of unknown shape: a press still held at
        the cue (a drag-style edit) → (press, its release); else the
        completed gesture before it (_completed_gesture). None without a
        press."""
        for index in range(at - 1, -1, -1):
            event = events[index]
            if event[1] == "up" and event[2] in _IMGUI_BUTTON:
                break                                   # completed before the cue
            if event[1] == "down" and event[2] in _IMGUI_BUTTON:
                button = event[2]
                up_index = next((i for i in range(index + 1, len(events))
                                 if events[i][1] == "up" and events[i][2] == button),
                                len(events) - 1)
                return index, up_index
        return cls._completed_gesture(events, at)

    @classmethod
    def _completed_gesture(cls, events, at):
        """(press index, last event index) of the COMPLETED mouse gesture a
        cue at `at` belongs to — a text edit's focus click, a window move's
        header drag — back over keystrokes / moves before the cue to the
        release and on to its press (moves between them are the click's
        jitter or the drag itself), forward over the keystrokes that follow
        the cue. None when no click precedes the cue."""
        release_index = None
        down_index = None
        for index in range(at - 1, -1, -1):
            event = events[index]
            if cls._keyboard_event(event) or event[1] in ("move", "change"):
                continue
            if event[1] == "up" and event[2] in _IMGUI_BUTTON and release_index is None:
                release_index = index
                continue
            if event[1] == "down" and event[2] in _IMGUI_BUTTON:
                down_index = index
            break
        if down_index is None:
            return None
        end_index = release_index if release_index is not None else down_index
        index = down_index + 1
        while index < len(events) and (cls._keyboard_event(events[index])
                                       or events[index][1] in ("move", "up")):
            if cls._keyboard_event(events[index]):
                end_index = max(end_index, index)
            index += 1
        return down_index, max(end_index, at - 1)

    @classmethod
    def _step_remap(cls):
        """Step the running value remap; on completion resume the tape after
        the gesture's release (its cue is the remap's own verification)."""
        task, generator, up_index, cue_index = cls._remap
        try:
            next(generator)
            return
        except StopIteration:
            pass
        except Exception as error:
            task.error = f"{type(error).__name__}: {error}"
        cls._remap = None
        if task.error:
            cls._cue_pending = cls.replaying.cues[cue_index]   # the failed command, for the report
            cls._cue_cursor = cue_index + 1
            cls.abort(f"command {cue_index + 1}: {task.error}")
            return
        events = cls.replaying.events
        speed = max(0.05, Toggles.Orchestrator.replay_speed)
        if getattr(task, "gates_only", False):
            # gates opened (or failed to open): the tape presses now, its
            # own timing re-based to the press so no time is owed - and
            # WHERE the press is now: the gesture's value shift is the
            # satisfied press_point's offset from the recorded press
            cls._replay_t0 = time.monotonic() - events[cls._replay_index][0] / speed
            down_index = cls._replay_index
            if task.press_point is not None and events[down_index][1] == "down":
                cls._injecting_index = down_index
                recorded_x, recorded_y = cls._event_xy(events[down_index], 3)
                cls._injecting_index = None
                shift = (task.press_point[0] - recorded_x, task.press_point[1] - recorded_y)
                if abs(shift[0]) > 0.5 or abs(shift[1]) > 0.5:
                    cls._remap_shift = (shift, down_index, up_index)
            return
        cls._replay_index = up_index + 1
        while (cls._cue_cursor < len(cls.replaying.cues)
               and cue_get(cls.replaying.cues[cls._cue_cursor], "at", 0) <= up_index):
            cls._cue_cursor += 1                    # the gesture's cues: verified by the servo
        # re-base the clock to the RELEASE's recorded time, so the pause
        # between the release and whatever follows is kept as recorded
        cls._replay_t0 = time.monotonic() - events[up_index][0] / speed

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
        cue_index = cls._cue_cursor - 1
        # First correction, for ANY cue with a target - whatever its kind or
        # stack: the window's preconditions for the target (the same
        # list it shows beside the command) are satisfied at the RECORDED
        # press point and the gesture replays. The effect the cue names is
        # the verification; how the click failed is not the engine's
        # business - the solver's is.
        if (cue_index not in cls._precondition_corrected and cue_has_target(cue)
                and cls._correct_via_preconditions(cue, cue_index)):
            return False
        if not cls._cue_corrected:
            if cue_get(cue, "stack") == "effects":
                # No re-aim for an effect cue: the click identifies itself by
                # its effect, so there is no tile to aim at - report the
                # missing effect (and what the solver tried, if it ran).
                cls.abort(f"effect missing: {cue_get(cue, 'kind')} "
                          f"'{cue_get(cue, 'name')}' after event {cue_get(cue, 'at')}"
                          + cls._precondition_note(cue_index))
                return False
            cls._cue_corrected = True
            cls._cue_wait = 0
            cls.status = f"correcting: {cue_get(cue, 'kind')} {cue_get(cue, 'name')}"
            cls._correct(cue)
            return False
        cls.abort(f"cue failed: expected {cue_get(cue, 'kind')} on "
                  f"'{cue_get(cue, 'name')}' after event {cue_get(cue, 'at')}"
                  + cls._precondition_note(cue_index))
        return False

    @classmethod
    def _precondition_note(cls, cue_index):
        attempts = cls._precondition_corrected.get(cue_index)
        if not attempts:
            return ""
        return " — preconditions tried: " + "; ".join(str(a) for a in attempts)

    @classmethod
    def _correct_via_preconditions(cls, cue, cue_index):
        """The window's preconditions as the correction: a gates-only
        ValueTask on the cue's target (path / gesture / press fraction
        exactly as `refresh_preconditions` lists them, the press point the
        RECORDED press re-anchored — where the tape is about to click),
        then the tape rewound to the gesture's press so it replays and the
        cue re-arms behind it. The task's own abort (no fix applied) lands
        as the cue's failure. False when the cue's gesture cannot be found
        (no press to replay)."""
        from src.lsd.gl_gui.view.playground.change_value import ValueTask
        orchestration = cls.replaying
        events = orchestration.events
        at = min(cue_get(cue, "at", 0), len(events))
        span = cls._cue_gesture(events, cue, at)
        if span is None:
            return False
        down_index, up_index = span
        if events[down_index][1] != "down":
            return False
        path = tuple(cue_get(cue, "chain") or [cue_get(cue, "name") or "?"])
        task = ValueTask(path, None, universe=cls._remap_universe)
        task.gates_only = True
        task.gesture = cue_gesture(cue)
        task.press_frac = cue_press_frac(cue)
        task.press_offset = cue_press_offset(cue)
        task.control = cue_control(cue)
        if task.press_offset is None and task.press_frac is None:
            cls._injecting_index = down_index
            task.press_point = cls._event_xy(events[down_index], 3)
            cls._injecting_index = None
        task.orchestration = orchestration
        task.start_frame = Melty.frame_count
        cls._precondition_corrected[cue_index] = task.attempts
        # rewind: the gesture replays once the gates are open, the cue
        # arms again after it (a second miss aborts)
        cls._cue_pending = None
        cls._cue_wait = 0
        cls._cue_corrected = False
        cls._cue_cursor = cue_index
        cls._replay_index = down_index
        cls._remap_shift = None
        cls._remap = (task, task.run(), up_index, cue_index)
        cls.status = (f"correcting: {cue_get(cue, 'kind')} {cue_get(cue, 'name')} · "
                      f"preconditions")
        request_render()
        return True

    @classmethod
    def _cue_satisfied(cls, cue):
        """A live change matching the cue's anchor appeared since replay
        start (and wasn't already claimed by an earlier identical cue)."""
        stack_name, kind = cue_get(cue, "stack"), cue_get(cue, "kind")
        display_name, new_repr = cue_get(cue, "name"), cue_get(cue, "new_repr")
        if stack_name == "effects":
            mark = cls._replay_marks.get("effects", 0)
            for entry in reversed(EffectLedger.entries):
                if entry.seq <= mark:
                    break
                if id(entry) in cls._matched_ids:
                    continue
                if entry.kind == kind and entry.name == display_name:
                    cls._matched_ids.add(id(entry))
                    return True
            # State evidence: a raise cue means "this window is at front" -
            # the publisher only fires on an ACTUAL restack (noise gating),
            # so a replayed click on an already-front window produces no
            # ledger entry. The state satisfies the cue just as truthfully.
            if kind == "raise" and _front_window_name() == display_name:
                return True
            return False
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
        cls._deferred_press = (input_id, x, y)   # pressed once the cursor settles

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
        cues = cls.replaying.cues if cls.replaying is not None else []
        anchor = (cue_get(cues[cue_index], "anchor")
                  if 0 <= cue_index < len(cues) else None)
        origin = cls.resolve_anchor_origin(anchor)
        cls._anchor_cache[cue_index] = origin
        return origin

    @classmethod
    def resolve_anchor_origin(cls, anchor):
        """Live (left, top) for a recorded anchor tuple — the cue's window
        tile against the live cache (nested windows too), then the
        registered window by name, else the RECORDED origin (reproduces the
        original absolute coordinates). Shared by replay (_anchor_origin)
        and command playback (re-anchoring a take's approach path)."""
        origin = (0.0, 0.0)
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
                # registered_windows is keyed by TILE ID, not name - scan
                # values for display name (a .get(name) here was a dead
                # fallback, triggered by a name-keyed test fake)
                for managed in (getattr(Melty, "registered_windows", None) or {}).values():
                    if str(getattr(managed, "name", "")).split("##")[0] == window_name:
                        window_ds = getattr(managed, "draw_state", None)
                        break
            if window_ds is not None:
                live_left = getattr(window_ds, "abs_left", None)
                live_top = getattr(window_ds, "abs_top", None)
                if live_left is not None and live_top is not None:
                    origin = (live_left, live_top)
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
        inherited = cls._inherited_anchor.get(cls._injecting_index)
        if inherited is not None:
            # absolute event: shift by the inherited anchor's displacement
            # since recording, so the offset stays continuous
            anchor = cue_get(cls.replaying.cues[inherited], "anchor")
            left, top = cls._anchor_origin(inherited)
            return x + (left - anchor[2]), y + (top - anchor[3])
        return x, y

    @classmethod
    def _trace_snapshot(cls):
        """One trace row per driving pump (state as this pump BEGINS, plus
        what the previous pump injected)."""
        try:
            io = imgui.get_io()
            io_pos = tuple(round(v, 1) for v in io.mouse_pos)
            io_down = bool(io.mouse_down[0])
        except Exception:
            io_pos, io_down = None, None
        focused = hovered_attr = None
        try:
            win = Melty.glfw_window
            if win is not None:
                focused = bool(glfw.get_window_attrib(win, glfw.FOCUSED))
                hovered_attr = bool(glfw.get_window_attrib(win, glfw.HOVERED))
        except Exception:
            pass
        hovered = getattr(Melty, "hovered_ds", None)
        handler = Melty.event_handler
        try:
            handler_down = bool(handler.is_down("left_mouse"))
        except Exception:
            handler_down = None
        target = getattr(cls._task, "trace_target", None)
        if target is None and cls._remap is not None:
            target = getattr(cls._remap[0], "trace_target", None)
        target_state = None
        if target is not None:
            # `blit_last`: the target's tile was served from the blit cache
            # on the frame just rendered - its imgui item was NOT submitted,
            # which is what drops imgui's ActiveId (and with it imgui_active,
            # the gate that keeps a window's header item off a window drag)
            served = getattr(target, "_blit_served_frame", None)
            target_state = dict(
                bounding=bool(getattr(target, "_bounding_hovered", False)),
                active=bool(getattr(target, "_imgui_is_active", False)),
                activated=bool(getattr(target, "_imgui_is_activated", False)),
                edited=bool(getattr(target, "_imgui_is_edited", False)),
                item_hovered=bool(getattr(target, "_imgui_is_item_hovered", False)),
                bvh_hover=id(target) in (getattr(Melty, "bvh_hover_ids", None) or ()),
                in_bvh=any(d is target for d in (getattr(Melty, "_bvh_id_to_ds", None) or {}).values()),
                blit_last=(served is not None and served >= Melty.frame_count - 1),
                served_frame=served,
                value=getattr(target, "_raw_input_value", None),
                rect=(getattr(target, "abs_left", None), getattr(target, "abs_top", None),
                      getattr(target, "width", None), getattr(target, "height", None)))
        # io is read BEFORE this pump's stamp_io (the pump runs first in
        # process_inputs, after the stock backend copied the REAL pointer in),
        # so real_io_* is the physical state, the virtual state is beside it.
        cls._trace.append(dict(
            pump=cls._pump_count, frame=Melty.frame_count, real_io_pos=io_pos, real_io_down=io_down,
            virtual=(round(cls._virtual_x, 1), round(cls._virtual_y, 1)),
            virtual_buttons=sorted(cls._virtual_buttons), real_down=sorted(cls._real_down),
            handler_down=handler_down,
            main_hovered=bool(getattr(Melty, "imgui_main_window_hovered", False)),
            imgui_active=bool(getattr(Melty, "imgui_active", False)),
            active_pending=bool(getattr(Melty, "imgui_active_pending", False)),
            on_drag=bool(getattr(Melty, "on_drag", False)),
            hovered_ds=str(getattr(hovered, "name", None)).split("##")[0] if hovered else None,
            glfw_focused=focused, glfw_hovered=hovered_attr,
            target=target_state, injected=list(cls._trace_injected)))
        cls._trace_injected = []

    @classmethod
    def _real_button_held(cls):
        """A physical mouse button is down. Stale entries (a release the
        platform ate — a compositor grab) are dropped through the backend's
        button probe, so a lost release can't wedge the engine."""
        if not cls._real_down:
            return False
        from src.lsd.gl_gui.events.input_handler import _BUTTON_PROBE
        probe = _BUTTON_PROBE.get("fn")
        for button in list(cls._real_down):
            if probe is not None and probe(button) is False:
                cls._real_down.discard(button)
        return bool(cls._real_down)

    @classmethod
    def press_ready(cls, point):
        """Whether a press at `point` may be injected NOW. If the cursor has
        never been moved this run, inject a move to `point` (so the press
        lands where the take says, not under the real pointer); either way
        require SETTLE_PUMPS pumps since the last injected move."""
        if cls._real_button_held():
            return False
        if cls._glide_queue:
            return False                     # still travelling
        if not cls._cursor_settled:
            cls._inject((0.0, "move", point[0], point[1]))
            return False
        # the press point is away from the cursor (a gate click left it on a
        # header; the tape resumed at its press): move there with the
        # mouse UP before pressing - a press that jumps lands on whatever
        # the last render had hovered at the OLD spot, and the recorded
        # drag then moves away (the window by its header, 09-01)
        distance = ((point[0] - cls._virtual_x) ** 2 + (point[1] - cls._virtual_y) ** 2) ** 0.5
        if distance > cls.PRESS_JUMP_PX and not cls._virtual_buttons:
            cls._inject((0.0, "move", point[0], point[1]))       # glide=True: queued up
            return False
        return cls._pump_count - cls._last_move_pump >= cls.SETTLE_PUMPS

    @classmethod
    def _inject(cls, event, glide=True):
        """Inject an event — through the continuous-mouse glide queue when a
        SYNTHESIZED positional event would jump the cursor (see
        _glide_queue). `glide=False` is the verbatim replay stream: its
        jumps are the recording's own. A move while a virtual button is
        held is a drag step (the servo paces those itself), never glided."""
        if Toggles.Orchestrator.continuous_mouse:
            if cls._glide_queue:
                if not glide:
                    # an explicitly paced injection (change_value's drag moves,
                    # presses, releases): it must never wait behind a tape
                    # glide - flush that glide to its endpoint first so
                    # sequence holds, then deliver
                    while cls._glide_queue:
                        item = cls._glide_queue.popleft()
                        if item[0] == "glide":
                            cls._deliver((0.0, "move", item[2][0], item[2][1]))
                        else:
                            cls._deliver(item)
                else:
                    cls._glide_queue.append(event)      # keep order behind a tape glide
                    return
            kind = event[1]
            if (glide and kind in ("move", "down", "up") and cls._cursor_settled
                    and not cls._virtual_buttons):
                slot = 2 if kind == "move" else 3
                x, y = cls._event_xy(event, slot)
                sx, sy = cls._virtual_x, cls._virtual_y
                distance = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
                if distance > cls.PRESS_JUMP_PX:
                    # a wall-clock glide: ("glide", from, to, t0, duration) -
                    # _drain_glide emits the eased position each pump until
                    # the duration elapses, then the queued event itself
                    from src.lsd.gl_gui.view.playground.change_value import glide_seconds
                    cls._glide_queue.append(("glide", (sx, sy), (x, y), time.monotonic(),
                                             glide_seconds(distance)))
                    cls._glide_queue.append(event)
                    return
        cls._deliver(event)

    @classmethod
    def _drain_glide(cls):
        """One glide step per pump; the instantaneous events queued behind
        it flow out in the same pump once the glide has landed."""
        while cls._glide_queue:
            item = cls._glide_queue[0]
            if item[0] == "glide":
                _tag, (sx, sy), (x, y), t0, duration = item
                fraction = min(1.0, (time.monotonic() - t0) / max(1e-6, duration))
                eased = fraction * fraction * (3.0 - 2.0 * fraction)
                cls._deliver((0.0, "move", sx + (x - sx) * eased, sy + (y - sy) * eased))
                if fraction < 1.0:
                    return                              # still travelling
                cls._glide_queue.popleft()
                continue                                # land: release what queued behind
            cls._glide_queue.popleft()
            cls._deliver(item)

    @classmethod
    def _deliver(cls, event):
        _dt, kind = event[0], event[1]
        cls._trace_injected.append(tuple(event[1:]))
        handler = Melty.event_handler
        cls._injecting = True
        try:
            if kind == "move":
                x, y = cls._event_xy(event, 2)
                cls._virtual_x, cls._virtual_y = x, y
                cls._last_move_pump = cls._pump_count
                cls._cursor_settled = True
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
        cls._remap = None
        cls._remap_shift = None
        cls._deferred_press = None
        cls._deferred_release = None
        cls.replaying = None
        cls._cue_pending = None
        cls._glide_queue.clear()
        cls._release_virtual()
        cls._release_cursor()
        cls.status = ""
        was_partial = cls._replay_partial
        cls._replay_end = None
        cls._replay_partial = False
        cls._clear_failure_for(orchestration)
        cls._record_success(orchestration)
        if orchestration.restore_on_finish and not was_partial:
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
        if cls.replaying is not None:
            cls._record_failure(cls.replaying, reason,
                                event_index=cls._replay_index,
                                cue_index=(cls._cue_cursor - 1
                                           if cls._cue_pending is not None else None))
        elif cls._task is not None:
            cls._record_failure(getattr(cls._task, "orchestration", None), reason)
        if cls._task is not None:
            cls._task.fail(reason)
            cls._task = None
        cls._remap = None
        cls._remap_shift = None
        cls._deferred_press = None
        cls._deferred_release = None
        cls.replaying = None
        cls._cue_pending = None
        cls._restore_steps = []
        cls._replay_end = None
        cls._replay_partial = False
        cls._glide_queue.clear()
        cls._release_virtual()
        cls._release_cursor()
        cls.status = ""
        notify(f"Replay stopped: {reason}", tint=(0.95, 0.6, 0.3, 1.0),
               tag="orchestrator", urgent=True)
        request_render()

    @classmethod
    def _record_failure(cls, orchestration, reason, event_index=None, cue_index=None):
        cls.last_failure = types.SimpleNamespace(
            orchestration=orchestration, reason=str(reason),
            event_index=event_index, cue_index=cue_index, when=time.time())
        success = cls.last_success
        if success is not None and success.orchestration is orchestration:
            cls.last_success = None
        # The full report also lands in a FILE (the error bar's copy is the
        # summary text): a 600-pump engine trace is too long for a clipboard
        # round-trip through a chat, and this way it can be read in place.
        try:
            import os
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return                       # the test-suite's failures must not clobber the studio's
            path = os.path.expanduser("~/.lsd/orchestrator_failure.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(failure_report(cls.last_failure))
        except Exception:
            pass
        return

    @classmethod
    def _record_success(cls, orchestration):
        if orchestration is None:
            return
        cls.last_success = types.SimpleNamespace(orchestration=orchestration,
                                                 when=time.time())

    @classmethod
    def _clear_failure_for(cls, orchestration):
        """A take that finishes successfully clears its own failure record."""
        failure = cls.last_failure
        if failure is not None and failure.orchestration is orchestration:
            cls.last_failure = None

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
        cls._cursor_settled = False
        cls._last_move_pump = -10
        cls._glide_queue.clear()
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
                cls._record_failure(getattr(task, "orchestration", None), task.error)
                notify(f"{task}: {task.error}", tint=(0.95, 0.6, 0.3, 1.0),
                       tag="orchestrator", urgent=True)
            else:
                cls._clear_failure_for(getattr(task, "orchestration", None))
                cls._record_success(getattr(task, "orchestration", None))
                notify(f"{task} done", tint=(0.5, 0.9, 0.5, 1.0), tag="orchestrator")
            task.finish()
        except Exception as error:
            cls._task = None
            cls._release_virtual()
            cls._release_cursor()
            cls.status = ""
            task.fail(f"{type(error).__name__}: {error}")
            cls._record_failure(getattr(task, "orchestration", None),
                                f"{type(error).__name__}: {error}")
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
    preconditions_changed = False
    if Orchestrator._precondition_watch and \
            Melty.frame_count - Orchestrator._precondition_frame >= Toggles.Orchestrator.precondition_refresh_frames:
        Orchestrator._precondition_frame = Melty.frame_count
        preconditions_changed = Orchestrator.refresh_preconditions(store)
    rows = tuple((key, orchestration.name, len(orchestration.events),
                  len(orchestration.cues), orchestration.restore_on_finish,
                  len(getattr(orchestration, "overrides", None) or {}))
                 for key, orchestration in store.orchestrations.items()) if store else ()
    signature = (Orchestrator.status, id(Orchestrator.recording),
                 id(Orchestrator.replaying), Orchestrator._replay_index,
                 id(Orchestrator.last_failure), id(Orchestrator.last_success),
                 bool(Orchestrator._restore_steps), rows)
    if signature != _last_signature or preconditions_changed:
        _last_signature = signature
        # if _window_draw_state is not None and Melty.cache is not None \
        #         and _
        # window_draw_state._tile_id is not None:
        #     Melty.cache.invalidate_up(_window_draw_state._tile_id, force=True)
        request_render()


@window(input_value=None, tint=(0.76, 0.44, 0.00), icon=f"",
        display_name="Orchestrator", initial={"width": 430, "height": 340})
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True, tint=(0.719, 0.478, 0.208))
def draw_orchestrator(input_value=None, draw_state=None, style_manager=None,
                      panel_state: OrchestratorPanelState = None,
                      renaming_key="", rename_text="",
                      left_mouse_down=False, left_mouse_double_clicked=False, **kwargs):
    # NOTE the click param names: "double_left_mouse_down" canonicalises
    # to the SAME (input_id, DOWN) key as left_mouse_down (DOWN has no double
    # promotion) but the per-frame name cache keeps one name per key - it
    # silently drops every plain press. DOUBLE_CLICKED is its own key.
    global _window_draw_state
    _window_draw_state = draw_state
    Orchestrator._precondition_watch = set()      # re-declared by the command rows below
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
    check_icon = f""                          # precondition rows: "target hittable"

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
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

    # Record New ALWAYS creates a fresh orchestration and records into it;
    # re-recording an existing row use that row's own record chip.
    record_label = f"{stop_icon}  Stop" if is_recording \
        else f"{record_icon}  Record New"
    record_width = imgui.calc_text_size(record_label)[0] + 2 * button_pad_x
    record_clicked, edge = _button(toolbar_x, toolbar_y, record_width, toolbar_height,
                                   record_label, record_tint if is_recording else draw_state.tint,
                                   active=is_recording)
    if record_clicked and not engine_busy:
        if is_recording:
            Orchestrator.stop_recording()
        else:
            from src.lsd.gl_gui.model.app_model import Orchestration
            target = Orchestration()
            target.name = f"Orchestration {len(orchestrations) + 1}"
            orchestrations[target.id] = target
            Orchestrator.start_recording(target)
        changed = True

    if engine_busy:
        abort_label = f"{stop_icon}  Abort"
        abort_width = imgui.calc_text_size(abort_label)[0] + 2 * button_pad_x
        abort_clicked, edge = _button(edge + button_gap, toolbar_y, abort_width,
                                      toolbar_height, abort_label, record_tint, active=True)
        if abort_clicked:
            Orchestrator.abort("stop button")

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
    precondition_indent = detail_indent + px(26.0)   # precondition rows sit under their command
    cue_tint = (0.85, 0.65, 0.2)                     # cue / effect rows (undo anchors)
    fail_tint = (0.92, 0.32, 0.3)                    # failed row / error bar
    success_tint = (0.32, 0.78, 0.42)                # success row wash / ok tag
    dim_color = _mix(draw_state.tint, dim_text_value, factor, text_saturation)
    fail_color = _mix(fail_tint, row_text_value + 0.2, 0.35, 1.2)
    success_color = _mix(success_tint, row_text_value + 0.1, 0.35, 1.2)
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
            orchestration.custom_name = True      # auto-name never overwrites this
            request_render()
            committed = True
        else:
            committed = False
        draw_state.renaming_key = ""
        return committed

    def _detail_rows(rows_list, playing, fail_at=None):
        """Both tabs render through here — same replay-progress highlight:
        the row being replayed brightens, finished rows dim, effect rows
        (cues / commands) wear the amber, the FAILED row (the cue that
        never verified / the event the replay stopped at) reads red."""
        nonlocal row_y
        for row in rows_list:
            start_index, end_index, row_kind, label, tag = row[:5]
            if clip is None or not (row_y + detail_row_height < clip[1] or row_y > clip[3]):
                running = playing and start_index <= Orchestrator._replay_index \
                    < max(end_index, start_index + 1)
                if row_kind == "cue":               # cue rows sit AT their index
                    failed_here = (fail_at is not None and not playing
                                   and start_index == fail_at)
                else:
                    failed_here = (fail_at is not None and not playing
                                   and start_index <= fail_at
                                   < max(end_index, start_index + 1))
                if failed_here:
                    label_color = fail_color
                elif running:
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

    def _command_rows(key, orchestration, tint, rows_list, playing_raw, cmd_cursor,
                      fail_cue_index=None):
        """Commands tab: each leaf-edit command's ARGUMENT is an inline edit
        box — the recorded value pre-filled (defaults reproduce the take
        exactly), an edit stores an override (amber text, × resets) and
        routes play through change_value with the edited value. Highlight:
        raw replay by event span, command playback by cue ordinal."""
        nonlocal row_y, changed
        overrides = getattr(orchestration, "overrides", None)
        if overrides is None:
            overrides = orchestration.overrides = {}
        for ordinal, row in enumerate(rows_list):
            start_index, end_index, row_kind, label, tag = row[:5]
            arg = row[5] if len(row) > 5 else None
            row_visible = clip is None or not (row_y + detail_row_height < clip[1]
                                               or row_y > clip[3])
            if row_visible:
                if cmd_cursor is not None:
                    running = ordinal == cmd_cursor
                    finished = ordinal < cmd_cursor
                else:
                    running = playing_raw and start_index <= Orchestrator._replay_index \
                        < max(end_index, start_index + 1)
                    finished = playing_raw \
                        and max(end_index, start_index + 1) <= Orchestrator._replay_index
                if fail_cue_index is not None and ordinal == fail_cue_index \
                        and not running:
                    label_color = fail_color
                elif running:
                    label_color = _mix(draw_state.tint, row_text_value + 0.4,
                                       factor, text_saturation)
                elif finished:
                    label_color = dim_color
                elif arg is None:
                    label_color = _mix(cue_tint, row_text_value, 0.4, 1.2)
                else:
                    label_color = _mix(draw_state.tint, row_text_value * 0.75,
                                       factor, text_saturation)
                # per-action run chip (the indent gutter): replays just THIS
                # command's event span - overrides remap its gesture as in
                # a full play - its cue verifying at the main
                run_clicked, _ = _button(row_left + px(10), row_y, px(18.0),
                                         px(16.0), play_icon, tint, icon_only=True)
                if run_clicked and not is_recording and not engine_busy:
                    Orchestrator.play(orchestration, start=start_index,
                                      end=max(end_index, start_index), generalize=True)
                    changed = True
                draw_list.add_text(row_left + detail_indent, row_y + text_nudge_y,
                                   _u32(label_color), label)
                if arg is not None:
                    override_key = str(ordinal)
                    modified = override_key in overrides
                    value = overrides.get(override_key, arg)
                    value_text = value if isinstance(value, str) else _format_value(value)
                    input_width = px(150.0) if isinstance(arg, str) else px(72.0)
                    input_x = (row_left + detail_indent
                               + imgui.calc_text_size(label)[0] + px(6))
                    imgui.set_cursor_screen_pos((input_x, row_y - px(1)))
                    imgui.push_item_width(input_width)
                    if modified:
                        imgui.push_style_color(imgui.COLOR_TEXT, 1.0, 0.75, 0.3, 1.0)
                    entered, text = imgui.input_text(
                        f"##orch-arg-{key}-{ordinal}", value_text, 128)
                    imgui.set_item_allow_overlap()
                    if modified:
                        imgui.pop_style_color()
                    imgui.pop_item_width()
                    if entered:
                        ok, parsed = parse_argument(text, arg)
                        if ok:
                            if parsed == arg:
                                overrides.pop(override_key, None)   # back to default
                            else:
                                overrides[override_key] = parsed
                            changed = True
                            request_render()
                    close_x = input_x + input_width + px(4)
                    draw_list.add_text(close_x, row_y + text_nudge_y,
                                       _u32(label_color), ")")
                    if modified:
                        reset_clicked, _ = _button(close_x + px(14), row_y, px(18.0),
                                                   px(16.0), delete_icon, tint,
                                                   icon_only=True)
                        if reset_clicked:
                            overrides.pop(override_key, None)
                            changed = True
                            request_render()
                if tag and arg is None:
                    tag_width = imgui.calc_text_size(tag)[0]
                    draw_list.add_text(row_right - px(4) - tag_width,
                                       row_y + text_nudge_y, _u32(dim_color), tag)
            row_y += detail_row_height
            if not cue_has_target(orchestration.cues[ordinal]):
                continue
            # ---- preconditions for this command (any cue with a target): one indented row per
            # unmet precondition (outermost first - the order the solver
            # takes them), the cheapest fix as its tag, a run chip that
            # satisfies preconditions up to and including THIS one ----
            Orchestrator._precondition_watch.add((key, ordinal))
            preconditions = Orchestrator._preconditions.get((key, ordinal))
            if preconditions is None:
                continue                                  # not listed yet (next sync)
            if not preconditions:
                if row_visible:
                    draw_list.add_text(row_left + precondition_indent, row_y + text_nudge_y,
                                       _u32(dim_color), f"{check_icon} target hittable")
                row_y += detail_row_height
                continue
            for precondition in preconditions:
                if clip is None or not (row_y + detail_row_height < clip[1] or row_y > clip[3]):
                    runnable = precondition["kind"] not in ("unresolved", "error", "hittable")
                    run_pre = False
                    if runnable:
                        run_pre, _ = _button(row_left + precondition_indent - px(22), row_y,
                                             px(18.0), px(16.0), play_icon, tint, icon_only=True)
                    if run_pre and not is_recording and not engine_busy:
                        from src.lsd.gl_gui.view.playground.change_value import precondition_task
                        cue = orchestration.cues[ordinal]
                        path = tuple(cue_get(cue, "chain") or [cue_get(cue, "name") or "?"])
                        Orchestrator.submit(precondition_task(
                            path, precondition["key"], orchestration=orchestration,
                            gesture=cue_gesture(cue), press_frac=cue_press_frac(cue)))
                        changed = True
                    pre_color = (fail_color if precondition["kind"] in ("unresolved", "error")
                                 else dim_color if precondition["kind"] == "hittable"
                                 else _mix(cue_tint, row_text_value, 0.4, 1.2))
                    label_text = precondition["label"]
                    if precondition["kind"] == "hittable":
                        label_text = f"{check_icon} {label_text}"
                    draw_list.add_text(row_left + precondition_indent, row_y + text_nudge_y,
                                       _u32(pre_color), label_text)
                    fixes = precondition.get("fixes") or []
                    if fixes:
                        fix_tag = f"fix: {fixes[0][1]}" + (f" (+{len(fixes) - 1})" if len(fixes) > 1 else "")
                        fix_width = imgui.calc_text_size(fix_tag)[0]
                        draw_list.add_text(row_right - px(4) - fix_width,
                                           row_y + text_nudge_y, _u32(dim_color), fix_tag)
                row_y += detail_row_height

    for key, orchestration in list(orchestrations.items()):
        row_rect = (row_left, row_y, row_right, row_y + row_height)
        header_visible = clip is None or not (row_rect[3] < clip[1]
                                              or row_rect[1] > clip[3])
        tint = orchestration.tint if isinstance(orchestration.tint, tuple) else draw_state.tint
        is_playing = Orchestrator.replaying is orchestration
        take_failure = (Orchestrator.last_failure
                        if (Orchestrator.last_failure is not None
                            and Orchestrator.last_failure.orchestration is orchestration)
                        else None)
        take_success = (Orchestrator.last_success
                        if (Orchestrator.last_success is not None
                            and Orchestrator.last_success.orchestration is orchestration
                            and take_failure is None)
                        else None)
        is_take = Orchestrator.recording is orchestration
        is_expanded = bool(expanded_map.get(key))
        renaming = renaming_key == key
        row_hovered = hover_ok and _in(row_rect, mouse_x, mouse_y)
        chip_y = row_y + (row_height - px(22)) / 2.0
        deleted = False

        if header_visible:
            bg_value = row_bg_value + (hover_bg_boost if row_hovered else 0.0)
            # every row floats on a soft drop shadow; a succeeded take's
            # row washes green (failure red wins - checked above)
            add_shadow((row_rect[0], row_rect[1], row_right - row_left, row_height),
                       corner_radius=corner, clip=clip)
            bg_tint = success_tint if take_success is not None else tint
            draw_list.add_rect_filled(row_rect[0], row_rect[1], row_rect[2], row_rect[3],
                                      _u32(_mix(bg_tint, bg_value + (0.03 if take_success else 0.0),
                                                factor, saturation)),
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
            # play = every command through change_value to its override or
            # its original value (a verbatim replay had no button worth
            # keeping — Lukas 09-01; `play(generalize=False)` still exists
            # for the tests and tooling)
            play_clicked, chip_edge = _button(chip_edge + px(2), chip_y, chip_width, px(22),
                                              play_icon, tint, icon_only=True)
            if play_clicked and not is_recording and not engine_busy:
                Orchestrator.play(orchestration, generalize=True)
                changed = True
            # per-row record chip: re-record INTO this orchestration (events
            # and cues replaced; the auto-name refreshes too, unless the
            # name was set by hand). Shows stop while THIS take records.
            record_chip_clicked, chip_edge = _button(
                chip_edge + px(2), chip_y, chip_width, px(22),
                stop_icon if is_take else record_icon,
                record_tint if is_take else tint,
                active=is_take, icon_only=True)
            if record_chip_clicked:
                if is_take:
                    Orchestrator.stop_recording()
                    changed = True
                elif not is_recording and not engine_busy:
                    Orchestrator.start_recording(orchestration)
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
                override_count = len(getattr(orchestration, "overrides", None) or {})
                tag = f"{orchestration.duration:g}s · {len(orchestration.events)} ev" \
                    + (f" · {override_count} edited" if override_count else "")
                if take_failure is not None:
                    tag += " · failed"
                elif take_success is not None:
                    tag += " · ok"
            tag_width = imgui.calc_text_size(tag)[0]
            tag_left = rename_left - button_gap - tag_width
            if is_playing:
                tag_color = _mix(tint, dim_text_value, factor, text_saturation)
            elif take_failure is not None:
                tag_color = fail_color
            elif take_success is not None:
                tag_color = success_color
            else:
                tag_color = _mix(tint, dim_text_value, factor, text_saturation)
            draw_list.add_text(tag_left,
                               row_y + (row_height - imgui.get_text_line_height()) / 2.0
                               + text_nudge_y, _u32(tag_color), tag)

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
            fail_at = None
            if take_failure is not None:
                if (take_failure.cue_index is not None
                        and take_failure.cue_index < len(orchestration.cues)):
                    fail_at = cue_get(orchestration.cues[take_failure.cue_index],
                                      "at", 0)
                else:
                    fail_at = take_failure.event_index
            if current_tab == "events":
                detail = group_events(orchestration.events, orchestration.cues)
                if not detail:
                    detail = [(0, 0, "", "no events yet — press Record", "")]
                _detail_rows(detail, is_playing, fail_at=fail_at)
            else:
                detail = generalized_commands(orchestration)
                if detail:
                    _command_rows(key, orchestration, tint, detail, is_playing, None,
                                  fail_cue_index=(take_failure.cue_index
                                                  if take_failure is not None else None))
                else:
                    _detail_rows([(0, 0, "", "no commands yet — effects appear "
                                             "as recording touches the undo stacks", "")],
                                 False)
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

    # ---- error bar: the full reason for the most recent failure ----
    failure = Orchestrator.last_failure
    if failure is not None:
        bar_top = row_y + px(4)
        bar_line_height = imgui.get_text_line_height()
        bar_pad_y = px(5.0)
        dismiss_reserve = px(4) + px(18.0) + button_gap
        message_wrap_width = max(px(60), (row_right - row_left) - px(16) - dismiss_reserve)
        failed_name = str(getattr(failure.orchestration, "name", None) or "task")
        where = ""
        if failure.cue_index is not None:
            where = f" (command {failure.cue_index + 1})"
        elif failure.event_index is not None:
            where = f" (event {failure.event_index})"
        message = f"{failed_name}{where}: {failure.reason}"
        message_lines = _wrap_text(message, message_wrap_width)
        bar_height = max(px(24.0), bar_pad_y * 2 + bar_line_height * len(message_lines))
        add_shadow((row_left, bar_top, row_right - row_left, bar_height),
                   corner_radius=corner, clip=clip)
        draw_list.add_rect_filled(row_left, bar_top, row_right, bar_top + bar_height,
                                  _u32(_mix(fail_tint, 0.045, 0.55, 1.1)),
                                  rounding=corner)
        message_rect = (row_left, bar_top, row_right - dismiss_reserve, bar_top + bar_height)
        message_hovered = hover_ok and _in(message_rect, mouse_x, mouse_y)
        # click anywhere on the message copies it (whole line, not just the
        # visible part); "copied" shows in place for a beat
        just_copied = (getattr(failure, "copied_at", None) is not None
                       and time.time() - failure.copied_at < 1.2)
        if click is not None and _in(message_rect, click[0], click[1]):
            try:
                imgui.set_clipboard_text(failure_report(failure))
            except Exception:
                pass
            failure.copied_at = time.time()
            just_copied = True
            request_render()
        if message_hovered:
            draw_list.add_rect_filled(message_rect[0], message_rect[1], message_rect[2],
                                      message_rect[3], _u32(_mix(fail_tint, 0.09, 0.55, 1.1)),
                                      rounding=corner)
        # the whole reason, WRAPPED to the bar (a solver failure lists every
        # fix it tried - one line clipped the part that mattered)
        shown_lines = (["debug report copied to clipboard"] if just_copied else message_lines)
        for line_index, line in enumerate(shown_lines):
            draw_list.add_text(row_left + px(8),
                               bar_top + bar_pad_y + line_index * bar_line_height + text_nudge_y,
                               _u32(success_color if just_copied else fail_color), line)
        if just_copied:
            request_render()                          # let the confirmation fade
        dismiss_clicked, _ = _button(row_right - px(4) - px(18.0),
                                     bar_top + (bar_height - px(16.0)) / 2.0,
                                     px(18.0), px(16.0), delete_icon, fail_tint,
                                     icon_only=True)
        if dismiss_clicked:
            Orchestrator.last_failure = None
            request_render()
        row_y = bar_top + bar_height + px(2)

    imgui.dummy(content_width, max(1.0, (row_y - origin_y) + row_gap))
    return changed, input_value


# The engine sees every real input through this one registration (a tap).
set_input_tap(Orchestrator.tap)

# Execution points publish observable, not-undoable effects here (an actual
# window raise, a fired flat_button) - the third cue source.
Melty.effect_hook = EffectLedger.note

# Ctrl+Shift+O anywhere: stop a recording / abort a replay - the mouse is
# busy driving (or being driven), so this must not depend on the window.
Melty.register_global_hotkey(glfw.KEY_O, glfw.MOD_CONTROL | glfw.MOD_SHIFT,
                             Orchestrator.hotkey, text_focus_ok=True)