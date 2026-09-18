"""Feedback-loop guard for the edge solver: bounded, always-on diagnostics.

While a mouse button is held, no edge may move faster than the pointer or
travel farther than the pointer has, whatever the collision rules say. An
edge either follows the hand (its own speed, in either direction: a blocked
frame edge pushes the opposite one) or stands still; contact frames may move
it part of the way. Anything beyond that is the solver feeding on its own
output (a nested frame carrying its child twice, a stub re-floored every
frame, a native ack folded back in as a new push).

Once per frame, after every window's edge pass, the guard samples every
registered edge in model screen coordinates and compares each step and total
travel with the pointer's motion budget. A violation writes one
``edge-motion-violation`` entry to the process's resize log (resize_trace)
with the offending edges, a summary of every window and the native model, and
the detection stack; for the following WATCH_FRAMES frames edge_constraints
also captures the stack of every solve that moves one of those edges, so the
next entry names the writer. One console line per entry, capped per gesture.

Change TOLERANCE_PX for the snap/rounding slack, RECENT_FRAMES for how many
frames of pointer motion one edge step may consume (native acknowledgements
land a frame or two late), Toggles.Melty.edge_motion_guard to switch it off.
"""
import time
import traceback

TOLERANCE_PX = 2.0
RECENT_FRAMES = 3
MAX_REPORTS_PER_GESTURE = 12
WATCH_FRAMES = 120
STACK_LIMIT = 30

# Survives hotswap (module re-exec reuses the existing dict).
_STATE = globals().get("_STATE") or {"gesture": None, "watch": set(), "watch_until": -1,
                                     "writes": [], "watching": False}
_BUTTONS = ("left_mouse", "right_mouse", "middle_mouse")


def _enabled():
    from meltygui.core.runtime.toggles import Toggles
    return bool(Toggles.Melty.edge_motion_guard)


def _button_down():
    from meltygui.core.melty import Melty
    handler = getattr(Melty, "event_handler", None)
    if handler is None:
        return False
    return any(handler.is_down(button) for button in _BUTTONS)


def _origin():
    import meltygui.core.windowing.os_frame as os_frame
    return (os_frame.applied_origin("x"), os_frame.applied_origin("y"))


def _pointer(origin):
    """The pointer in model screen coordinates: the surface's applied
    origin plus the handler's content-local cursor."""
    from meltygui.core.melty import Melty
    x, y = Melty.event_handler.cursor()
    return (origin[0] + float(x), origin[1] + float(y))


def _window_label(window):
    return str(getattr(window, "name", None) or getattr(window, "id", "?"))[:60]


def _edge_positions(origin):
    """``{id(edge): (label, screen position, axis)}`` for every edge the
    solver owns this frame: each window's frame pair and layout lists, and
    the native frame pair. Shared edge objects appear once."""
    import meltygui.core.windowing.os_frame as os_frame
    import meltygui.core.layout.column_core as column_core
    out = {}
    for window in os_frame._all_windows():
        if (os_frame._unmanaged(window) or getattr(window, "closed", False)
                or not getattr(window, "expanded", True) or not window.width or not window.height):
            continue
        base = (float(window.abs_left or 0), float(window.abs_top or 0))
        name = _window_label(window)
        for axis, i in (("x", 0), ("y", 1)):
            lists = []
            frame = column_core._frame(window, axis)
            if frame:
                lists.append((f"{name} frame", frame))
            for key, (_owner, edges) in list(column_core._views(window, axis).items()):
                lists.append((f"{name} {key}", edges))
            for label, edges in lists:
                for k, edge in enumerate(edges):
                    if id(edge) in out or not isinstance(edge, dict) or edge.get(axis) is None:
                        continue
                    out[id(edge)] = (f"{label}[{k}]", origin[i] + base[i] + float(edge[axis]), axis)
    if os_frame._enabled():
        for axis in ("x", "y"):
            for role, edge in zip(("near", "far"), os_frame.edges(axis)):
                if id(edge) not in out and edge.get(axis) is not None:
                    out[id(edge)] = (f"native {role}", float(edge[axis]), axis)
    return out


def _new_gesture(frame, pointer, origin, positions):
    return {"start_frame": frame, "frame": frame, "pointer": pointer, "origin": origin,
            "path": [0.0, 0.0], "recent": [], "start": dict(positions), "prev": dict(positions),
            "reports": 0}


def check_frame():
    """Once per frame from Melty.end_frame, after every edge pass."""
    try:
        _check_frame()
    except Exception:
        # Diagnostics must never turn an otherwise valid frame into a failure.
        pass


def _check_frame():
    from meltygui.core.melty import Melty
    frame = Melty.frame_count
    if _STATE["watching"] and frame > _STATE["watch_until"]:
        _STATE["watching"], _STATE["watch"], _STATE["writes"] = False, set(), []
    if not _enabled() or not _button_down():
        _STATE["gesture"] = None
        return
    origin = _origin()
    pointer = _pointer(origin)
    positions = _edge_positions(origin)
    gesture = _STATE["gesture"]
    if gesture is None:
        _STATE["gesture"] = _new_gesture(frame, pointer, origin, positions)
        return
    if gesture["frame"] == frame:
        return
    gesture["frame"] = frame
    delta = (pointer[0] - gesture["pointer"][0], pointer[1] - gesture["pointer"][1])
    # A landed native acknowledgement shifts the applied origin: motion the
    # pointer already paid for, so it widens the budget rather than counting
    # against the edges that ride the surface.
    landed = (abs(origin[0] - gesture["origin"][0]), abs(origin[1] - gesture["origin"][1]))
    gesture["pointer"], gesture["origin"] = pointer, origin
    gesture["recent"].append((abs(delta[0]) + landed[0], abs(delta[1]) + landed[1]))
    del gesture["recent"][:-RECENT_FRAMES]
    for i in range(2):
        gesture["path"][i] += abs(delta[i]) + landed[i]
    allowance = (sum(r[0] for r in gesture["recent"]), sum(r[1] for r in gesture["recent"]))
    violations = []
    for eid, (label, pos, axis) in positions.items():
        i = 0 if axis == "x" else 1
        previous = gesture["prev"].get(eid)
        if previous is None:
            # First seen during this gesture (a layout seeded, a window
            # opened): its baseline is where it appeared.
            gesture["start"][eid] = gesture["prev"][eid] = (label, pos, axis)
            continue
        step = pos - previous[1]
        travel = pos - gesture["start"][eid][1]
        kind = None
        if abs(step) > allowance[i] + TOLERANCE_PX:
            kind = "faster than the pointer"
        elif abs(travel) > gesture["path"][i] + TOLERANCE_PX:
            kind = "farther than the pointer"
        if kind:
            violations.append({"edge": label, "id": eid, "axis": axis, "kind": kind,
                               "previous": previous[1], "now": pos, "step": step,
                               "travel": travel, "allowance": allowance[i],
                               "pointer_path": gesture["path"][i]})
        gesture["prev"][eid] = (label, pos, axis)
    if violations:
        _report(gesture, violations, delta, allowance, frame)


def _summary():
    """The app's geometry state, metadata only: never view values."""
    import meltygui.core.windowing.os_frame as os_frame
    import meltygui.core.layout.column_core as column_core
    windows = []
    for window in os_frame._all_windows():
        entry = {"name": _window_label(window), "window_pos": getattr(window, "window_pos", None),
                 "width": window.width, "height": window.height,
                 "abs": (getattr(window, "abs_left", None), getattr(window, "abs_top", None)),
                 "min": (getattr(window, "min_width", None), getattr(window, "min_height", None)),
                 "closed": getattr(window, "closed", False),
                 "frame_pinned": bool(getattr(window, "_frame_pinned", False)),
                 "unmanaged": os_frame._unmanaged(window),
                 "parent": _window_label(window.parent_window) if getattr(window, "parent_window", None) else None}
        for axis in ("x", "y"):
            pending = getattr(window, column_core._REGISTRY[axis][1], None) or []
            entry[f"pending_{axis}"] = [(id(item[0]), item[1], bool(item[2]) if len(item) > 2 else None)
                                        for item in pending]
            entry[f"layouts_{axis}"] = {str(key): [round(float(e.get(axis, 0)), 1) for e in edges]
                                        for key, (_o, edges) in list(column_core._views(window, axis).items())}
        windows.append(entry)
    state = os_frame._STATE
    native = {"mode": state.get("mode"), "generation": state.get("generation"),
              "expected": list(state.get("expected") or ()), "unapplied": list(state.get("unapplied") or ()),
              "unapplied_far": list(state.get("unapplied_far") or ()),
              "edges": {axis: [e.get(axis) for e in state["edges"][axis]] for axis in ("x", "y")},
              "screen": {axis: [e.get(axis) for e in state["screen"][axis]] for axis in ("x", "y")}
              if state.get("screen") else None,
              "gestures": {axis: sorted(g.get("totals", {}).keys()) if isinstance(g, dict) else str(g)
                           for axis, g in (state.get("gestures") or {}).items()},
              "pin_rebases": len(state.get("pin_rebases") or {})}
    return windows, native


def _report(gesture, violations, delta, allowance, frame):
    gesture["reports"] += 1
    if gesture["reports"] > MAX_REPORTS_PER_GESTURE:
        return
    windows, native = _summary()
    writes = [w for w in _STATE["writes"] if w["ids"] & {v["id"] for v in violations}]
    _STATE["watch"] |= {v["id"] for v in violations}
    _STATE["watch_until"] = frame + WATCH_FRAMES
    _STATE["watching"] = True
    _STATE["writes"] = []
    _emit({"frame": frame, "gesture_start_frame": gesture["start_frame"],
           "pointer_delta": delta, "allowance": allowance, "pointer_path": list(gesture["path"]),
           "violations": violations, "windows": windows, "native": native,
           "solver_writes": writes, "stack": traceback.format_stack(limit=STACK_LIMIT)})


def _emit(report):
    from meltygui.core.diagnostics import resize_trace
    import meltygui.core.windowing.os_frame as os_frame
    roots = os_frame._root_windows()
    resize_trace.record("edge-motion-violation", roots[0] if roots else None, **report)
    worst = max(report["violations"], key=lambda v: abs(v["step"]))
    print(f"[edge-motion-guard] frame {report['frame']}: {len(report['violations'])} edge(s) moved "
          f"beyond the pointer; worst {worst['edge']} {worst['axis']} {worst['kind']} "
          f"step {worst['step']:+.1f} (allowed {worst['allowance']:.1f}) travel {worst['travel']:+.1f} "
          f"(pointer {worst['pointer_path']:.1f}); see resize_trace log", flush=True)


def watching():
    return _STATE["watching"]


def note_solve(moved_ids):
    """From edge_constraints.solve_edge while watching: the stack of a solve
    that moved a watched edge, kept for the next report."""
    hits = set(moved_ids) & _STATE["watch"]
    if not hits:
        return
    _STATE["writes"].append({"ids": hits, "time": time.time(),
                             "stack": traceback.format_stack(limit=STACK_LIMIT)[:-1]})
    del _STATE["writes"][:-50]
