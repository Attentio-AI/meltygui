"""Observe rendered DrawState geometry without changing collision decisions.

The wrapper records its final geometry, including cache replays. No registry
walk, extra redraw, or DrawState field is needed. Skipped cached descendants
are deliberately not sampled: they did not run a wrapper this frame.

Reports are *candidates*, not assertions of invalid collision behavior. A
single-frame step beyond the hand's budget or an out-and-back excursion while
the hand does not reverse deserves a trace even when a multi-frame edge budget
hides it. Native origin changes widen the step budget; they remain in the log.
Only metadata is retained, separately per surface, with two release frames and
bounded before/after history. Surface state disappears when its owner closes.
"""
import math
import traceback
import weakref

from meltygui.core.diagnostics import edge_motion_guard, resize_trace

# Raise this to ignore larger pixel-rounding excursions. This is a diagnostic
# threshold, never a collision tolerance or an allowance in the solver.
JITTER_PX = 2.0
HISTORY_FRAMES = 5
FOLLOWUP_FRAMES = 2
MAX_REPORTS_PER_GESTURE = 12
MAX_VIEWS_PER_REPORT = 20
FIELDS = ("width", "height", "abs_top", "abs_left")
AXES = (0, 1, 1, 0)

# In-place hotswap retains diagnostic history without retaining draw states.
_SURFACES = globals().get("_SURFACES", weakref.WeakKeyDictionary())
_STUDIO = globals().get("_STUDIO", {})
_ERRORS = globals().get("_ERRORS", set())


def _context():
    from meltygui.core.melty import Melty
    surface = edge_motion_guard._surface()
    state = _STUDIO if surface is None else _SURFACES.setdefault(surface, {})
    frame = Melty.frame_count
    if state.get("frame") == frame:
        return state
    down = edge_motion_guard._button_down() if edge_motion_guard._enabled() else False
    # frame_count is process-wide: other surfaces may render between these
    # samples. Adjacency means successive completed frames of THIS surface.
    if ((state and not state.get("checked"))
            or (down and not state.get("down"))
            or not edge_motion_guard._enabled()):
        state.clear()
    tail = FOLLOWUP_FRAMES if down else max(0, state.get("tail", 0) - 1)
    active = down or state.get("tail", 0) > 0
    state.update(frame=frame, down=down, tail=tail, active=active, samples={}, checked=False)
    if active:
        origin = edge_motion_guard._origin()
        handler = Melty.event_handler
        state.update(origin=origin, pointer=edge_motion_guard._pointer(origin),
                     buttons=[button for button in edge_motion_guard._BUTTONS
                              if handler is not None and handler.is_down(button)],
                     surface=edge_motion_guard._surface_title(surface))
        state.setdefault("start_frame", frame)
        state.setdefault("history", [])
        state.setdefault("reports", 0)
        state.setdefault("followups", [])
    else:
        # Release all per-view history once the short release tail is over.
        for key in ("history", "followups", "start_frame", "reports"):
            state.pop(key, None)
    return state


def _error(stage):
    detail = traceback.format_exc()
    key = (stage, detail.splitlines()[-1])
    if key not in _ERRORS:
        _ERRORS.add(key)
        resize_trace.record("draw-state-jitter-error", None, error=True, operation=stage)
        print(f"[draw-state-jitter] {stage}: {key[1]}", flush=True)


def sample(draw_state, renderer=None):
    """Called at the wrapper's exit, after its size/position/cache work."""
    try:
        state = _context()
        if (not state["active"] or draw_state.frame_count < 3
                or draw_state.closed or not draw_state.expanded):
            return
        geometry = tuple(float(getattr(draw_state, field)) for field in FIELDS)
        parent = draw_state.parent_window
        state["samples"][id(draw_state)] = {
            "geometry": geometry, "id": str(draw_state.id),
            "name": str(draw_state.name)[:120],
            "parent": id(parent) if parent is not None else None,
            "renderer": (f"{renderer.__module__}.{renderer.__qualname__}"
                         if renderer is not None else None),
        }
    except Exception:
        _error("sample")


def _candidates(history):
    """Compare adjacent rendered samples, never stale or newly appeared views."""
    if len(history) < 2:
        return []
    previous, current = history[-2:]
    before = history[-3] if len(history) >= 3 else None
    candidates = []
    for identity, sample in current["samples"].items():
        old = previous["samples"].get(identity)
        if old is None or old["id"] != sample["id"]:
            continue
        earlier = before["samples"].get(identity) if before else None
        changes = []
        for index, (field, axis) in enumerate(zip(FIELDS, AXES)):
            value, last = sample["geometry"][index], old["geometry"][index]
            step = value - last
            pointer_step = current["pointer"][axis] - previous["pointer"][axis]
            origin_step = current["origin"][axis] - previous["origin"][axis]
            # A span can change at both ends. Positions have a single edge.
            factor = 2 if field in ("width", "height") else 1
            allowance = factor * (abs(pointer_step) + abs(origin_step)) + JITTER_PX
            smooth_lag = False
            if earlier is not None and earlier["id"] == sample["id"]:
                first_step = last - earlier["geometry"][index]
                prior_pointer = previous["pointer"][axis] - before["pointer"][axis]
                prior_origin = previous["origin"][axis] - before["origin"][axis]
                # Some wrappers consume a divider's solved position on the
                # following frame. A continuing, same-size step paid for by
                # that prior pointer sample is smooth settling, not a jump.
                # Do not pool frame budgets: a new step or a reversal still
                # needs its own report even if recent hand travel was large.
                smooth_lag = (step * first_step > 0 and abs(step - first_step) <= JITTER_PX
                              and abs(step) <= factor * (abs(prior_pointer) + abs(prior_origin)) + JITTER_PX)
            kinds = []
            if not math.isfinite(value):
                kinds.append("non-finite geometry")
            elif math.isfinite(last) and abs(step) > allowance and not smooth_lag:
                kinds.append("step exceeds frame motion")
            if earlier is not None and earlier["id"] == sample["id"]:
                first = earlier["geometry"][index]
                first_step = last - first
                prior_pointer = previous["pointer"][axis] - before["pointer"][axis]
                # Both legs must exceed rounding noise; a real hand reversal
                # explains an out-and-back gesture. Stops/starts alone do not
                # count as spikes. Keep native motion visible, not smoothed.
                if (first_step * step < 0 and min(abs(first_step), abs(step)) > JITTER_PX
                        and prior_pointer * pointer_step >= 0):
                    kinds.append("one-frame reversal without pointer reversal")
            if kinds:
                changes.append(dict(field=field, kinds=kinds, step=step,
                                    pointer_step=pointer_step, origin_step=origin_step,
                                    allowance=allowance))
        if changes:
            candidates.append(dict(identity=identity, id=sample["id"], name=sample["name"],
                                   parent=sample["parent"], renderer=sample["renderer"], changes=changes))
    return candidates


def _history_for(history, identities):
    return [{**{key: value for key, value in frame.items() if key != "samples"},
             "views": [{"identity": identity, **sample,
                        "geometry": dict(zip(FIELDS, sample["geometry"]))}
                       for identity, sample in frame["samples"].items() if identity in identities]}
            for frame in history]


def check_frame():
    """Flush once after all wrappers, independently of the edge guard's cap."""
    try:
        _check_frame()
    except Exception:
        _error("check_frame")


def _check_frame():
    state = _context()
    if not state["active"] or state["checked"]:
        return
    state["checked"] = True
    frame = {key: state[key] for key in ("frame", "pointer", "origin", "buttons", "samples")}
    history = state["history"]
    history.append(frame)
    del history[:-HISTORY_FRAMES]
    # Write the frames after a detection as well, including release. Link them
    # by the detection frame so a persistent jump is as inspectable as a spike.
    pending = []
    for detection, identities, remaining in state["followups"]:
        resize_trace.record("draw-state-jitter-followup", None, surface=state["surface"],
                            detection_frame=detection, samples=_history_for([frame], identities))
        if remaining > 1:
            pending.append((detection, identities, remaining - 1))
    state["followups"] = pending
    candidates = _candidates(history)
    if not candidates or state["reports"] >= MAX_REPORTS_PER_GESTURE:
        return
    state["reports"] += 1
    selected = candidates[:MAX_VIEWS_PER_REPORT]
    identities = {candidate["identity"] for candidate in selected}
    identities.update(candidate["parent"] for candidate in selected if candidate["parent"] is not None)
    resize_trace.record("draw-state-jitter", None, surface=state["surface"],
                        gesture_start_frame=state["start_frame"], candidates=selected,
                        candidate_count=len(candidates), report=state["reports"],
                        report_limit=MAX_REPORTS_PER_GESTURE,
                        samples=_history_for(history, identities))
    state["followups"].append((state["frame"], identities, FOLLOWUP_FRAMES))
    print(f"[draw-state-jitter] {state['surface'] or 'studio'} frame {state['frame']}: "
          f"{len(candidates)} view(s), {selected[0]['name']} "
          f"{', '.join(change['field'] for change in selected[0]['changes'])}; "
          f"see resize trace ({state['reports']}/{MAX_REPORTS_PER_GESTURE})", flush=True)
