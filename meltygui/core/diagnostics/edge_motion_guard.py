"""Feedback-loop guard for the edge solver: bounded, always-on diagnostics.

While a mouse button is held, every edge either moves at the pointer's speed
(in either direction: a blocked frame edge pushes the opposite one) or stands
still, whatever the collision rules say; no edge may move faster than the
pointer or travel farther than the pointer has. That holds for the native
(OS) frame pair too: under a held button the hand is the only reference, and
a native request in flight never suspends judgement - a push into the display
wall files one every frame. A native resize with NO button held (the
compositor's own grab, a programmatic placement) is driven by the native edge
that moved instead: the same rules apply with that edge's motion in place of
the pointer's, and that edge alone is not judged against itself. Speed is judged in chunks of
CHUNK_PX of pointer motion rather than single frames, so a slow hand (a pixel
or two a frame) is judged as well as a fast one: in each chunk an edge stands
still, matches the pointer, or moves part of the way. A part-way chunk is a
contact (the edge meeting or leaving a barrier) only at the start or end of a
moving stretch; one between two moving chunks is an edge lagging the hand.
Anything beyond
that is the solver feeding on its own output (a nested frame carrying its
child twice, a stub re-floored every frame, a native ack folded back in as a
new push) or losing part of the hand's motion on the way to the edge.

During a right-button drag an edge moving the pointer's way must also have a
reason to move: it is the latched edge, it is within NEAR_POINTER_PX of the
pointer, or it is in contact (a cell behind it at its minimum span, a cell
ahead of it at its maximum, within CONTACT_SLACK_PX). Motion opposite to the
pointer is the edge-push flip, and motion back toward where the gesture found
the edge is the sticky reversal; both are exempt. Anything else is an edge
pushed when nothing pushes it.

Once per frame, after every window's edge pass, the guard samples every
registered edge in model screen coordinates and compares each step and total
travel with the pointer's motion budget: window frame pairs, every column and
row layout registered on a window or on an ordinary view, and the native frame
pair. Each native surface (an app's root and its child windows) keeps its own
gesture; entries name the surface. A violation writes one
``edge-motion-violation`` entry to the process's resize log (resize_trace)
with the offending edges, a summary of every window and the native model, and
the detection stack; for the following WATCH_FRAMES frames edge_constraints
also captures the stack of every solve that moves one of those edges, so the
next entry names the writer. One console line per entry, capped per gesture.

The pointer is read in model screen coordinates (applied surface origin plus
the content-local position). While a native move is in flight the origin and
the local pointer can shift on different frames, so single frames over- or
under-state the hand's motion by one surface step: the upper bounds are
judged over RECENT_FRAMES frames, and a landed origin shift widens the budget
(motion the pointer already paid for).

Change TOLERANCE_PX for the snap/rounding slack, RECENT_FRAMES for how many
frames of pointer motion one edge step may consume, CHUNK_PX for the pointer
motion one speed judgement spans, MATCH_FRACTION for how much of it an edge
must cover to count as following, Toggles.Melty.edge_motion_guard to switch
it off.
"""
import time
import traceback

TOLERANCE_PX = 2.0
RECENT_FRAMES = 3
CHUNK_PX = 24.0
MATCH_FRACTION = 0.85
NEAR_POINTER_PX = 48.0
CONTACT_SLACK_PX = 4.0
# A gesture survives this long without driver motion: a compositor resize
# delivers its configures in bursts, and a verdict needs several chunks of
# one gesture.
IDLE_S = 0.6
_now = time.monotonic
MAX_REPORTS_PER_GESTURE = 12
WATCH_FRAMES = 120
STACK_LIMIT = 30

# Survives hotswap (module re-exec reuses the existing dict).
_STATE = globals().get("_STATE") or {"gestures": {}, "watch": set(), "watch_until": -1,
                                     "writes": [], "watching": False, "errors": set(), "owners": {}}
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


def _right_down():
    from meltygui.core.melty import Melty
    handler = getattr(Melty, "event_handler", None)
    return bool(handler is not None and handler.is_down("right_mouse"))


def _latched(owners):
    """Edges a right-drag resize is driving this frame: the column and row
    targets core_render latched on the windows receiving the drag."""
    ids = set()
    for window in owners:
        for name in ("_resize_target_edge", "_resize_target_row"):
            edge = getattr(window, name, None)
            if edge is not None:
                ids.add(id(edge))
    return ids


def _in_contact(owner, axis, edge, direction):
    """Is ``edge`` moving in ``direction`` (+1 / -1) on ``axis`` because a
    cell of its owner's layouts pushes it (the cell behind it at its floor)
    or pulls it (the cell ahead at its cap)? Cells come from the owner's
    registered lists and frame pair, floors and caps from their specs."""
    import meltygui.core.layout.column_core as column_core
    from meltygui.core.layout import edge_constraints
    views = _registry(owner, axis)
    # Only layout cells make contact. The window's own frame pair is
    # registered as a cell floored at its declared minimum; a window held at
    # that minimum would otherwise pass its far edge's motion to its near
    # edge as "contact" while the columns between them still have slack.
    keys = [k for k in views if k != getattr(owner, "id", None)]
    lists = [views[k][1] for k in keys]
    specs = getattr(owner, column_core._REGISTRY[axis][3], None) or {}
    spec_list = [specs.get(k) for k in keys]
    graph = edge_constraints.EdgeGraph(column_core._cells_from_lists(lists, axis, specs=spec_list))
    behind = graph.behind.get(id(edge), ()) if direction > 0 else graph.ahead.get(id(edge), ())
    ahead = graph.ahead.get(id(edge), ()) if direction > 0 else graph.behind.get(id(edge), ())
    for other, floor, _cap in behind:                       # pushed: the cell it leaves is packed
        if abs(edge[axis] - other[axis]) <= floor + CONTACT_SLACK_PX:
            return True
    for other, _floor, cap in ahead:                        # pulled: the cell it enters is full
        if cap is not None and abs(other[axis] - edge[axis]) >= cap - CONTACT_SLACK_PX:
            return True
    return False


def _native_resize_live():
    """A native resize nobody's Melty button is driving: a configure landed
    within the settle window (Melty.os_resize_live)."""
    from meltygui.core.melty import Melty
    return bool(Melty.os_resize_live())


def _native_motion(previous, positions):
    """Per axis, the largest step any native frame edge made since the last
    sample: the driver of a resize with no button held."""
    motion = [0.0, 0.0]
    for eid, (label, pos, axis) in positions.items():
        if not label.startswith("native "):
            continue
        earlier = previous.get(eid)
        if earlier is None:
            continue
        i = 0 if axis == "x" else 1
        step = pos - earlier[1]
        if abs(step) > abs(motion[i]):
            motion[i] = step
    return tuple(motion)


def _origin():
    import meltygui.core.windowing.os_frame as os_frame
    return (os_frame.applied_origin("x"), os_frame.applied_origin("y"))


def _unapplied():
    """Native near-edge motion requested but not yet landed, per axis."""
    import meltygui.core.windowing.os_frame as os_frame
    return tuple(os_frame._STATE.get("unapplied") or (0.0, 0.0))


def _surface():
    """The native surface drawing this frame (None: the studio's window).
    Per-window Melty state is swapped in by Surface.activate, so each
    surface's frame sees its own windows, handler and native model."""
    try:
        from meltygui.core.windowing.surface import Surface
    except Exception:
        return None
    return Surface.active


def _surface_title(surface):
    return getattr(surface, "title", None) if surface is not None else None


def _pointer(origin):
    """The pointer in model screen coordinates: the surface's applied
    origin plus the content-local position the input handler builds its
    drag events from (Melty.get_latest_mouse, the same source as a drag
    event's total). The handler's own cursor field lags it."""
    from meltygui.core.melty import Melty
    x, y = Melty.get_latest_mouse()
    return (origin[0] + float(x), origin[1] + float(y))


def _window_label(window):
    return str(getattr(window, "name", None) or getattr(window, "id", "?"))[:60]


def _registry(window, axis):
    """A window's layout registry on ``axis``; empty for a window that has
    not run an edge pass yet (collapsed, never drawn)."""
    import meltygui.core.layout.column_core as column_core
    return getattr(window, column_core._REGISTRY[axis][0], None) or {}


def _layout_owners():
    """Every draw state whose registries hold edges for this surface: its
    windows (frame pairs and the layouts registered on them) and any
    ordinary view that served as a layout's coordinate owner, provided its
    window ancestry leads into this surface. Windows come first."""
    import meltygui.core.windowing.os_frame as os_frame
    import meltygui.core.layout.column_core as column_core
    owners, seen = [], set()
    windows = os_frame._all_windows()
    window_ids = {id(w) for w in windows}
    for window in windows:
        if (os_frame._unmanaged(window) or getattr(window, "closed", False)
                or not getattr(window, "expanded", True) or not window.width or not window.height
                or (getattr(window, "frame_count", 3) or 0) < 3):
            continue                       # a window still fitting itself on its first frames
        seen.add(id(window))
        owners.append(window)
    # The layouts' own record of who holds registries: probing every draw
    # state in the app for them cost most of a millisecond per drag frame.
    for ds in column_core.layout_owners():
        if id(ds) in seen or not (getattr(ds, "_edge_views", None)
                                                or getattr(ds, "_row_views", None)):
            continue
        if getattr(ds, "closable", False) or getattr(ds, "closed", False):
            continue                       # a window: covered above, or not this surface's
        node, depth = getattr(ds, "parent_window", None), 0
        while node is not None and id(node) not in window_ids and depth < 64:
            node = getattr(node, "parent_window", None)
            depth += 1
        if node is None and windows:
            continue                       # no ancestor in this surface
        seen.add(id(ds))
        owners.append(ds)
    return owners


def _edge_positions(origin):
    """``{id(edge): (label, screen position, axis)}`` for every edge the
    solver owns this frame: each owner's frame pair and column/row layout
    lists, and the native frame pair. Shared edge objects appear once."""
    import meltygui.core.windowing.os_frame as os_frame
    import meltygui.core.layout.column_core as column_core
    out, acknowledged = {}, set()
    owners = _STATE["owners"] = {}
    for window in _layout_owners():
        base = (float(getattr(window, "abs_left", 0) or 0), float(getattr(window, "abs_top", 0) or 0))
        name = _window_label(window)
        for axis, i in (("x", 0), ("y", 1)):
            lists = []
            frame = column_core._frame(window, axis)
            # A surface-bound frame pair mirrors the native pair through
            # fixed gaps and follows the compositor's acknowledgement, a
            # frame or two behind the model; layouts drawn on that root
            # adopt the same edge objects. The native pair below is the
            # solver's own output for that degree of freedom.
            if frame and os_frame.frame_binding(window, axis) is not None:
                acknowledged.update(id(edge) for edge in frame)
            elif frame:
                lists.append((f"{name} frame", frame))
            for key, (_owner, edges) in list(_registry(window, axis).items()):
                lists.append((f"{name} {key}", edges))
            for label, edges in lists:
                for k, edge in enumerate(edges):
                    if (id(edge) in out or id(edge) in acknowledged
                            or not isinstance(edge, dict) or edge.get(axis) is None):
                        continue
                    out[id(edge)] = (f"{label}[{k}]", origin[i] + base[i] + float(edge[axis]), axis)
                    owners[id(edge)] = (window, edge)
    if os_frame._enabled():
        for axis in ("x", "y"):
            for role, edge in zip(("near", "far"), os_frame.edges(axis)):
                if id(edge) not in out and edge.get(axis) is not None:
                    out[id(edge)] = (f"native {role}", float(edge[axis]), axis)
    return out


def _new_gesture(frame, pointer, origin, positions, driver="pointer"):
    # driver_pos: where the driver is, in one running coordinate: the pointer
    # itself, or the native edges' accumulated motion for a native resize.
    return {"driver": driver, "driver_pos": (0.0, 0.0), "last_motion": _now(),
            "start_frame": frame, "frame": frame, "pointer": pointer, "origin": origin,
            "path": [0.0, 0.0], "recent": [], "start": dict(positions), "prev": dict(positions),
            "chunk": {"pointer": (0.0, 0.0), "pointer_at_start": pointer,
                      "path": [0.0, 0.0],
                      "native": [0.0, 0.0], "native_max": [0.0, 0.0],
                      "start": {eid: pos for eid, (_l, pos, _a) in positions.items()}},
            "chunks": {}, "reports": 0}


def check_frame():
    """Once per frame from Melty.end_frame, after every edge pass."""
    try:
        _check_frame()
    except Exception:
        # Diagnostics must never turn an otherwise valid frame into a failure,
        # but a guard that cannot run must not look like a clean gesture:
        # each distinct failure is printed once and logged.
        text = traceback.format_exc()
        key = text.strip().splitlines()[-1]
        if key not in _STATE.setdefault("errors", set()):
            _STATE["errors"].add(key)
            print(f"[edge-motion-guard] disabled by an error: {key}", flush=True)
            try:
                from meltygui.core.diagnostics import resize_trace
                resize_trace.record("edge-motion-guard-error", None, error=True)
            except Exception:
                pass


def _check_frame():
    from meltygui.core.melty import Melty
    frame = Melty.frame_count
    if _STATE["watching"] and frame > _STATE["watch_until"]:
        _STATE["watching"], _STATE["watch"], _STATE["writes"] = False, set(), []
    if not _enabled():
        _STATE["gestures"].clear()
        return
    surface = _surface()
    gestures = _STATE["gestures"]
    button = _button_down()
    # One gesture spans both drivers: an edge drag can cross the window
    # edge, where the press is dropped and picked up again, and a native
    # resize keeps landing after the release. Resetting on each flip would
    # never accumulate the chunks a verdict needs.
    driver = "pointer" if button else ("native" if _native_resize_live() else None)
    gesture = gestures.get(id(surface))
    if driver is None:
        # A released hand ends its gesture at once; only a native resize
        # (configures arriving in bursts) may pause and continue.
        if gesture is None or gesture["driver"] != "native" or _now() - gesture["last_motion"] > IDLE_S:
            gestures.pop(id(surface), None)
            return
        driver = gesture["driver"]                       # a pause inside the gesture
    origin = _origin()
    pointer = _pointer(origin)
    positions = _edge_positions(origin)
    if gesture is None:
        gestures[id(surface)] = _new_gesture(frame, pointer, origin, positions, driver)
        # One entry per gesture proves the guard watched it, even when it
        # has nothing to report.
        from meltygui.core.diagnostics import resize_trace
        roots = _roots()
        resize_trace.record("edge-motion-guard-armed", roots[0] if roots else None,
                            surface=_surface_title(surface), driver=driver, pointer=pointer,
                            edges=len(positions), owners=len(_layout_owners()),
                            labels=sorted(label for label, _pos, _axis in positions.values())[:60])
        return
    if gesture["frame"] == frame:
        return
    gesture["frame"] = frame
    gesture["driver"] = driver
    native_step = _native_motion(gesture["prev"], positions)
    pointer_delta = (pointer[0] - gesture["pointer"][0], pointer[1] - gesture["pointer"][1])
    # A landed native acknowledgement shifts the applied origin: motion the
    # pointer already paid for, so it widens the budget rather than counting
    # against the edges that ride the surface.
    origin_shift = (abs(origin[0] - gesture["origin"][0]), abs(origin[1] - gesture["origin"][1]))
    delta, landed = [0.0, 0.0], [0.0, 0.0]
    for i in range(2):
        if button:
            # A held button makes the HAND the only reference. A native edge
            # that moves under it is judged like every other edge: taken as
            # the reference, a runaway OS edge set its own speed limit and
            # travel budget and was never reported (Lukas 09-18).
            delta[i], landed[i] = pointer_delta[i], origin_shift[i]
        elif abs(native_step[i]) > TOLERANCE_PX:
            # No button: the compositor's own resize. The native edge that
            # moved is the reference on this axis (its motion already
            # includes any origin shift).
            delta[i] = native_step[i]
    gesture["pointer"], gesture["origin"] = pointer, origin
    gesture["driver_pos"] = (gesture["driver_pos"][0] + delta[0], gesture["driver_pos"][1] + delta[1])
    gesture["right"] = button and _right_down()
    gesture["latched"] = _latched(_layout_owners()) if gesture["right"] else set()
    if any(abs(d) > TOLERANCE_PX for d in delta):
        gesture["last_motion"] = _now()
    gesture["recent"].append((abs(delta[0]) + landed[0], abs(delta[1]) + landed[1]))
    del gesture["recent"][:-RECENT_FRAMES]
    for i in range(2):
        gesture["path"][i] += abs(delta[i]) + landed[i]
    allowance = (sum(r[0] for r in gesture["recent"]), sum(r[1] for r in gesture["recent"]))
    speed = gesture["recent"][-1]
    chunk = gesture["chunk"]
    for i in range(2):
        chunk["path"][i] += abs(delta[i])
        chunk["native"][i] += abs(native_step[i])
        chunk["native_max"][i] = max(chunk["native_max"][i], abs(native_step[i]))
    violations = []
    for eid, (label, pos, axis) in positions.items():
        i = 0 if axis == "x" else 1
        previous = gesture["prev"].get(eid)
        if previous is None:
            # First seen during this gesture (a layout seeded, a window
            # opened): its baseline is where it appeared.
            gesture["start"][eid] = gesture["prev"][eid] = (label, pos, axis)
            continue
        if not button and label.startswith("native ") and abs(native_step[i]) > TOLERANCE_PX:
            gesture["prev"][eid] = (label, pos, axis)      # the reference itself
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
                               "pointer_step": speed[i], "chunks": None,
                               "travel": travel, "allowance": allowance[i],
                               "pointer_path": gesture["path"][i]})
        gesture["prev"][eid] = (label, pos, axis)
    for i in range(2):
        if chunk["path"][i] < CHUNK_PX:
            continue
        violations.extend(_close_chunk(gesture, i, gesture["driver_pos"], positions, frame))
    if violations:
        _report(gesture, violations, delta, allowance, frame)


def _close_chunk(gesture, i, pointer, positions, frame):
    """One chunk of pointer motion on axis ``i`` is complete: class every
    edge's motion over it and report a part-way chunk between two moving
    chunks. Returns the violations."""
    chunk = gesture["chunk"]
    axis = "xy"[i]
    # Judged against the native edges' own motion only when they ARE the
    # driver (no button held): under the hand they are edges like any other.
    against_native = gesture["driver"] == "native" and chunk["native"][i] > TOLERANCE_PX
    net = abs(pointer[i] - chunk["pointer"][i])
    # A reversal inside the chunk leaves the motion unmeasurable. Our own
    # native request in flight does NOT: a push into the display wall files
    # one every frame, and leaving those chunks unjudged hid exactly the
    # gestures this guard exists for (Lukas 09-18).
    judged = net >= CHUNK_PX / 2
    previous_net = gesture.setdefault("previous_net", [None, None])[i]
    # An edge dragged through a layout handle trails the hand by one frame;
    # under acceleration that reads as part way against this chunk alone,
    # so it is judged against the smaller of this and the previous chunk.
    reference = min(net, previous_net) if previous_net else net
    gesture["previous_net"][i] = net if judged else None
    violations = []
    for eid, (label, pos, edge_axis) in positions.items():
        if edge_axis != axis or (against_native and label.startswith("native ")):
            continue
        start = chunk["start"].get(eid)
        chunk["start"][eid] = pos
        if start is None:
            gesture["chunks"].pop(eid, None)
            continue
        if not judged:
            gesture["chunks"].pop(eid, None)
            continue
        moved = abs(pos - start)
        if moved <= TOLERANCE_PX:
            kind = "still"
        elif moved >= reference * MATCH_FRACTION - TOLERANCE_PX:
            kind = "match"
        else:
            kind = "partial"
        if kind != "still" and gesture.get("right") and not against_native:
            reason = _push_reason(gesture, eid, label, pos, start, i)
            if reason is None:
                violations.append({"edge": label, "id": eid, "axis": axis, "kind": "pushed without contact",
                                   "previous": start, "now": pos, "step": pos - start,
                                   "pointer_step": net, "chunks": None,
                                   "travel": pos - gesture["start"][eid][1], "allowance": net,
                                   "pointer_path": gesture["path"][i],
                                   "pointer_distance": abs(gesture["pointer"][i] - pos)})
        history = gesture["chunks"].setdefault(eid, [])
        history.append({"kind": kind, "edge": moved, "pointer": net, "frame": frame})
        del history[:-3]
        # A part-way chunk between two moving chunks is lag only when the
        # edge is also short over each pair of neighbours: an edge trailing
        # the hand by one frame is part way in one chunk and ahead in the
        # next, and the pairs add up.

        def short(a, b):
            return a["edge"] + b["edge"] < (a["pointer"] + b["pointer"]) * MATCH_FRACTION - TOLERANCE_PX

        if (len(history) == 3 and history[1]["kind"] == "partial"
                and history[0]["kind"] != "still" and history[2]["kind"] != "still"
                and short(history[0], history[1]) and short(history[1], history[2])):
            violations.append({"edge": label, "id": eid, "axis": axis, "kind": "slower than the pointer",
                               "previous": start, "now": pos, "step": pos - start,
                               "pointer_step": net, "chunks": list(history),
                               "travel": pos - gesture["start"][eid][1], "allowance": net,
                               "pointer_path": gesture["path"][i]})
            del history[:]
    chunk["pointer"] = tuple(pointer[j] if j == i else chunk["pointer"][j] for j in range(2))
    chunk["pointer_at_start"] = tuple(gesture["pointer"][j] if j == i else chunk["pointer_at_start"][j]
                                      for j in range(2))
    chunk["path"][i] = 0.0
    chunk["native"][i] = 0.0
    chunk["native_max"][i] = 0.0
    return violations


def _push_reason(gesture, eid, label, pos, start, i):
    """Why an edge moving the pointer's way in a right-drag may move, or
    None: it is the latched edge, near the pointer, in contact through a
    cell, moving opposite to the pointer, or not one of a layout's edges."""
    pointer_move = gesture["pointer"][i] - gesture["chunk"]["pointer_at_start"][i]
    step = pos - start
    if pointer_move * step < 0:
        return "opposite"                       # the edge-push flip
    travel_before = start - gesture["start"][eid][1]
    if travel_before * step < 0:
        return "restoring"                      # sticky reversal: back toward where the gesture found it
    if eid in gesture.get("latched", ()):
        return "latched"
    if abs(gesture["pointer"][i] - pos) <= NEAR_POINTER_PX:
        return "near pointer"
    owner = _STATE.get("owners", {}).get(eid)
    if owner is None:
        return "not a layout edge"
    if _in_contact(owner[0], "xy"[i], owner[1], 1 if step > 0 else -1):
        return "contact"
    return None


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
                                        for key, (_o, edges) in list(_registry(window, axis).items())}
        windows.append(entry)
    state = os_frame._STATE
    native = {"mode": state.get("mode"), "generation": state.get("generation"),
              "expected": list(state.get("expected") or ()), "unapplied": list(state.get("unapplied") or ()),
              "unapplied_far": list(state.get("unapplied_far") or ()),
              "edges": {axis: [e.get(axis) for e in state["edges"][axis]] for axis in ("x", "y")},
              "screen": {axis: [e.get(axis) for e in state["screen"][axis]] for axis in ("x", "y")}
              if state.get("screen") else None,
              # The native gesture's displacement so far, [near, far] per axis
              # (os_frame.solve keeps a list; reading it as a dict lost the
              # whole summary exactly when an OS edge was being pushed).
              "gestures": {axis: ([round(float(t), 1) for t in g.get("totals") or ()]
                                  if isinstance(g, dict) else str(g))
                           for axis, g in (state.get("gestures") or {}).items()},
              "pin_rebases": len(state.get("pin_rebases") or {})}
    return windows, native


def _report(gesture, violations, delta, allowance, frame):
    gesture["reports"] += 1
    if gesture["reports"] > MAX_REPORTS_PER_GESTURE:
        return
    try:
        windows, native = _summary()
    except Exception:
        # The violation itself is the point; a summary failure is noted, not fatal.
        windows, native = [{"summary_error": traceback.format_exc()}], None
    writes = [w for w in _STATE["writes"] if w["ids"] & {v["id"] for v in violations}]
    _STATE["watch"] |= {v["id"] for v in violations}
    _STATE["watch_until"] = frame + WATCH_FRAMES
    _STATE["watching"] = True
    _STATE["writes"] = []
    _emit({"frame": frame, "surface": _surface_title(_surface()), "driver": gesture["driver"],
           "gesture_start_frame": gesture["start_frame"],
           "pointer_delta": delta, "allowance": allowance, "pointer_path": list(gesture["path"]),
           "violations": violations, "windows": windows, "native": native,
           "solver_writes": writes, "stack": traceback.format_stack(limit=STACK_LIMIT)})


def _roots():
    import meltygui.core.windowing.os_frame as os_frame
    return os_frame._root_windows()


def _emit(report):
    from meltygui.core.diagnostics import resize_trace
    roots = _roots()
    resize_trace.record("edge-motion-violation", roots[0] if roots else None, **report)
    worst = max(report["violations"], key=lambda v: abs(abs(v["step"]) - v["pointer_step"]))
    print(f"[edge-motion-guard] {report['surface'] or 'studio'} frame {report['frame']}: "
          f"{len(report['violations'])} edge(s) off the "
          f"{'pointer' if report['driver'] == 'pointer' else 'native edge'}'s motion; worst {worst['edge']} {worst['axis']} {worst['kind']} "
          f"step {worst['step']:+.1f} (pointer {worst['pointer_step']:.1f}, allowed up to "
          f"{worst['allowance']:.1f}) travel {worst['travel']:+.1f} (pointer {worst['pointer_path']:.1f}); "
          f"see resize_trace log", flush=True)


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
