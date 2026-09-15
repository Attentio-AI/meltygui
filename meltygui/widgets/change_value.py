"""change_value(path, to): drive a UI value to a target through REAL input
events, generalized from recorded demonstrations.

    change_value(("33071d", "alpha"), 7.5)
    change_value("Lora Window B/[1]/lora_dropout", 0.2)
    change_value(("990001", "name"), "some random new text")

The path is a selector (selectors.py) — leaf-anchored, nesting-free. The
target's EDITOR KIND (the cue's applicability signature) picks an archetype
executor; the recorded take of that kind supplies the press point as a
fraction of the demonstrated leaf's rect, re-applied to the resolved
target's live rect — which is what makes one alpha demonstration drive
lora_dropout, or any float field anywhere.

Archetypes:
    drag  (draw_float / draw_int) — press, PROBE a few px to measure the
          live gain from the coalescing undo Change (the held drag updates
          one Change per frame: the undo stack is the sensor), then a
          secant servo to the target. Recorded gain is never trusted;
          per-field speed=, clamps and nonlinearity are absorbed by
          re-measuring every step. A stall (clamp) releases and aborts.
    text  (draw_str / draw_text) — click to focus, synthesized clear
          (select-all + delete, End+backspace fallback), payload typed from
          `to` in the event SHAPE a real keystroke produces (key + char),
          exact-match verification with one clear-and-retype retry.
    toggle (draw_bool) — click unless already at the target.

Preconditions are a small SOLVER (see `PRECONDITIONS` below): before the
executor runs, the target must be hittable at its press point. The unmet
precondition found first along the containment path — a closed window, a
collapsed ancestor, the target outside its scroll viewport, the press
point covered by another window — names the EFFECT KINDS that satisfy it;
every fix's MECHANISM is discovered from recorded takes (an effect cue of
that kind + the press offset relative to the view it happened on:
"expand", "raise", "scroll", a WindowMoveChange's header press), never
coded here, and generalizes to any subject of the same kind by that
rect-relative offset. Ranking is one number — the disturbance cost table
`Toggles.Orchestrator.fix_costs` (re-pick the press point 0, raise 1,
scroll / expand 2, move a window 3, +1 for the cue's anchor window) — the
cheapest applicable fix runs first, the predicate is re-checked (verify by
effect, never by assumption), the next candidate runs when it did not
help. Every attempt is logged into the abort message. A closed WINDOW (the
dock-row lookup is deferred) or a missing demonstration aborts with the
unmet need named.

Execution: a ValueTask generator stepped once per frame by
Orchestrator.pump (real input muted, Esc aborts, same contract as replay).
`task = change_value(...)` returns immediately; task.wait() blocks for test
harnesses that drive frames themselves.
"""
import threading
import time

from src.lsd.gl_gui import window_api as glfw

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_views.core_undo import UndoManager
from src.lsd.gl_gui.view.playground.orchestrator import Orchestrator, cue_get
from src.lsd.gl_gui.view.playground.selectors import (
    resolve, parse, format_path, ancestor_chain, display_name, full_name, NoMatch, Ambiguous)

# editor kind -> archetype. A take demonstrated on ANY field of a kind
# drives every field of that kind; add a row here when a new leaf editor
# gets a up.
_ARCHETYPE_BY_EDITOR = {"draw_float": "drag", "draw_int": "drag",
                        "draw_str": "text", "draw_text": "text",
                        "draw_bool": "toggle"}

# Archetypes that MUST have a recorded demonstration (the take carries the
# press fraction and, for text, the event shape). "toggle" degenerates to a
# centered click and runs take-less.
_NEEDS_TAKE = {"drag", "text"}


class _Abort(Exception):
    """Raised inside a task generator to stop with an honest message."""


class _PreconditionDone(Exception):
    """A precondition-only run (ValueTask.until) reached its precondition."""


def change_value(path, to, within=None, universe=None, eps=None):
    """Drive the value at `path` to `to` with real input. Returns the
    ValueTask immediately; the engine pumps it per frame. task.wait() for
    blocking callers, task.error for the outcome."""
    return Orchestrator.submit(ValueTask(path, to, within=within,
                                         universe=universe, eps=eps))


# ── the library: takes indexed by what they demonstrate ──────────────────

def orchestration_store():
    root = getattr(getattr(Melty, "vis", None), "root", None)
    store = getattr(root, "orchestrations", None)
    return getattr(store, "orchestrations", None) or {}


def terminal_cue(take):
    """The take's last edit-stack Change cue — its demonstrated effect."""
    for cue in reversed(getattr(take, "cues", []) or []):
        if cue_get(cue, "kind") == "Change":
            return cue
    return None


def take_for(archetype):
    """A recorded take whose terminal cue's editor maps to `archetype` —
    one demonstration per widget kind covers the whole UI."""
    for take in orchestration_store().values():
        cue = terminal_cue(take)
        if cue is not None and _ARCHETYPE_BY_EDITOR.get(cue_get(cue, "editor")) == archetype:
            return take
    return None


def effect_offset(kind, editor=None):
    """How a demonstration produced effect `kind` ("expand", "raise",
    "scroll", …): the press offset from the top-left of the view the
    effect happened on (the effect cue's press_offset). The control sits
    at the same offset on every subject of that kind — the expand arrow on
    every collection header, the title on every window header — so ONE
    demonstration serves any subject OF THAT KIND: with `editor` (the
    subject's view function) a demonstration from the same editor wins
    (a collapsed WINDOW's chevron is not where a collection's is — the
    collection's offset read as "clipped" on a 32 px window header, 09-01),
    any other demonstration of the effect is the fallback. A take recorded
    before press_offset existed still carries press_frac + leaf_rect (the
    press as a fraction of the subject's rect, and that rect's size):
    frac × size IS the pixel offset — without this, nested gates whose name
    no recorded take matched aborted with "no expand demonstration" (09-01)."""
    matched = fallback = derived = None
    for take in orchestration_store().values():
        for cue in getattr(take, "cues", []) or []:
            if cue_get(cue, "kind") != kind:
                continue
            offset = cue_get(cue, "press_offset")
            if not offset:
                frac, rect = cue_get(cue, "press_frac"), cue_get(cue, "leaf_rect")
                if derived is None and frac and rect and rect[2] > 0 and rect[3] > 0:
                    derived = (float(frac[0]) * float(rect[2]),
                               float(frac[1]) * float(rect[3]))
                continue
            offset = (float(offset[0]), float(offset[1]))
            if editor is not None and cue_get(cue, "editor") == editor:
                if matched is None:
                    matched = offset
            elif fallback is None:
                fallback = offset
    if matched is not None:
        return matched
    if fallback is not None:
        return fallback
    return derived


def expand_offset(editor=None):
    return effect_offset("expand", editor=editor)


def move_offset():
    """Where a recorded window move grabbed its window: the WindowMoveChange
    cue anchors on the moved window at its PRE-drag corner and the take's
    events tagged with that cue are relative to it, so the tagged `down`
    event IS the header press offset. One recorded move teaches how to
    grab any window; the drag delta is computed per use (gain is 1 px/px,
    no servo). None when no move was recorded."""
    for take in orchestration_store().values():
        cues = getattr(take, "cues", []) or []
        for index, cue in enumerate(cues):
            if cue_get(cue, "kind") != "WindowMoveChange":
                continue
            for event in getattr(take, "events", []) or []:
                if (len(event) > 5 and event[1] == "down" and event[2] == "left_mouse"
                        and event[5] == index):
                    return (float(event[3]), float(event[4]))
    return None


def raise_offset():
    """A raise demonstration's press offset on its window — a recorded
    raise effect first, else the header press of a recorded move (pressing
    a header raises the window too)."""
    return effect_offset("raise") or move_offset()


def fragment_for_gate(gate_name=None):
    """Fallback for demonstrations recorded before press_offset: a take
    whose expand effect names THIS gate (a fragment for a different
    collection clicks the wrong place), else the legacy bool-Change shape."""
    candidates = []
    for take in orchestration_store().values():
        for cue in reversed(getattr(take, "cues", []) or []):
            kind = cue_get(cue, "kind")
            if kind == "expand":
                candidates.append(("exact" if cue_get(cue, "name") == gate_name else "other",
                                   take))
                break
            if kind == "Change":
                if (cue_get(cue, "value_type") == "bool"
                        and cue_get(cue, "new") in (True, "True")
                        and cue_get(cue, "editor") not in _ARCHETYPE_BY_EDITOR):
                    candidates.append(("legacy", take))
                break                       # a leaf-edit take, not an expand take
    for wanted in ("exact", "legacy"):      # never an expand fragment for ANOTHER gate
        for grade, take in candidates:
            if grade == wanted:
                return take
    return None


# ── live-tree helpers (module-level so tests can monkeypatch) ────────────

def _editor_of(ds):
    return getattr(getattr(ds, "_view_func", None), "__name__", None)


def _current_value(ds):
    return getattr(ds, "_raw_input_value", None)


def _live_rect(ds):
    return (getattr(ds, "abs_left", 0) or 0, getattr(ds, "abs_top", 0) or 0,
            getattr(ds, "width", 0) or 0, getattr(ds, "height", 0) or 0)


def _reachable(ds):
    """The one universal guard: is this target actually hittable? The
    STRUCTURAL check is authoritative — a closed window or a collapsed
    ancestor makes the target unreachable whatever else says (a collapsed
    subtree leaves stale hit boxes in the BVH and a stale rect on the
    draw_state, which once passed a BVH-first check and pressed into
    nothing). Geometry must be present too; BVH membership adds nothing
    beyond that."""
    for node in [ds] + ancestor_chain(ds):
        if getattr(node, "closed", False):
            return False
        if node is not ds and getattr(node, "expanded", True) is False:
            return False
    left, top, width, height = _live_rect(ds)
    return width > 0 and height > 0


def resolve_recorded(path, within=None, universe=None):
    """Resolve a RECORDED chain by its minimal unique suffix (capture
    maximal, match minimal): a cue's full chain reaches up to the root
    ("Main Window"), which is never a cached tile and so never resolves as
    a segment — the full chain as a path is only as resolvable as its most
    fragile ancestor name. Try suffixes shortest→longest; the first that
    resolves uniquely wins (Ambiguous keeps extending). Returns (ds, suffix
    tried last); raises NoMatch carrying the deepest resolvable prefix so
    the abort names what DID resolve."""
    segments = parse(path)
    last_error = None
    for length in range(1, len(segments) + 1):
        suffix = segments[-length:]
        try:
            return resolve(suffix, within=within, universe=universe), suffix
        except Ambiguous as ambiguous:
            last_error = ambiguous
            continue
        except NoMatch as missing:
            last_error = missing
            break
    if isinstance(last_error, Ambiguous):
        raise last_error
    raise NoMatch(segments, hint=f"leaf {format_path(segments[-1:])} not live")


# ── preconditions ─────────────────────────────────────────────────────────
#
# Each precondition: predicate → (kind, node). `PRECONDITIONS` lists the
# fix kinds that satisfy each kind; the fixes' mechanisms come from takes
# (effect_offset / move_offset), the ranking from Toggles.Orchestrator.fix_costs.

PRECONDITIONS = {
    "closed": ("open",),                 # a window in the chain is closed (note: deferred)
    "collapsed": ("expand",),            # an ancestor collection is folded
    "outside": ("scroll",),              # the target is scrolled out of its viewport
    "clipped": ("re_pick", "scroll"),    # the press point is clipped, part of the view shows
    "obscured": ("re_pick", "raise", "move"),   # another window covers the press point
}


def _window_of(ds):
    node, steps = ds, 0
    while node is not None and steps < 64:
        parent = getattr(node, "parent_window", None)
        if parent is None or parent is node:
            return node
        node, steps = parent, steps + 1
    return node


def _hits_at(x, y):
    """Every live view under (x, y), front first — the same hit test the
    input handler presses into (tests substitute a fake)."""
    try:
        return list(Melty.bvh_query(x, y))
    except Exception:
        return []


def _visible_rect(ds):
    """The target's rect ∩ its clip (what can actually be pressed); empty
    when scrolled out of its viewport."""
    left, top, width, height = _live_rect(ds)
    rect = (left, top, left + width, top + height)
    clip = getattr(ds, "abs_clip_rect", None)
    if clip is None:
        return rect
    return (max(rect[0], clip[0]), max(rect[1], clip[1]),
            min(rect[2], clip[2]), min(rect[3], clip[3]))


def _front_window_at(x, y):
    """The frontmost melty window under (x, y). Read from the renderer's
    own paint order (`Melty.paint_ordered_ds`, back → front, rebuilt from
    live state every frame) — NOT the BVH: a blit-cached window that was
    raised or moved over the target keeps its hit boxes / z stamp until
    its wrapper next runs, so the BVH ranked the covered field in front of
    the window covering it and the press went nowhere (a nudge of the
    obscurer "fixed" it, 09-01). The BVH hit test stays the fallback for
    a world without a paint order (tests)."""
    ordered = getattr(Melty, "paint_ordered_ds", None) or []
    for window in reversed(ordered):
        if _window_hidden(window):
            continue
        left, top, right, bottom = _visible_rect(window)     # what is DRAWN, not the raw width
        if left <= x <= right and top <= y <= bottom:
            return window
    hits = _hits_at(x, y)
    return _window_of(hits[0]) if hits else None


def _window_hidden(window):
    """A window that paints nothing: closed, culled off-screen, or nested
    inside a closed / COLLAPSED ancestor. A collapsed window itself is NOT
    hidden — its header strip (the expand chevron!) is drawn and hittable
    at its live rect. `abs_closed` reads True for a collapsed window, so
    it must not be the test here: with it, a collapsed 'Loras' was skipped
    by the obscurer scan, the root window won the point under its own
    chevron and the expand fix reported "covered by 'Main Window'" for an
    uncovered button (Lukas 09-01)."""
    node, steps = window, 0
    while node is not None and steps < 64:
        if getattr(node, "closed", False) and getattr(node, "closable", True):
            return True
        if getattr(node, "_hidden_offscreen", False):
            return True
        parent = getattr(node, "parent_window", None)
        if parent is None or parent is node:
            return False
        if getattr(parent, "expanded", True) is False:
            return True                      # inside a collapsed window: not drawn
        node, steps = parent, steps + 1
    return False


def _window_chain(ds):
    """Every window the target sits in, nearest first, up to its root."""
    chain, node, steps = [], getattr(ds, "parent_window", None), 0
    while node is not None and steps < 64:
        chain.append(node)
        parent = getattr(node, "parent_window", None)
        if parent is None or parent is node:
            break
        node, steps = parent, steps + 1
    return chain or [ds]


def _first_unmet(ds, press_point, header=False):
    """The outermost unmet precondition on the way to `ds` at `press_point`:
    (kind, node) — the node is what the fix acts on (the closed window, the
    folded collection, the target for "outside", the OBSCURING window) —
    or None when the target is hittable there. `header`: the press is a
    header gesture on `ds` itself (raise / move) — a collapsed window with
    no grabbable header is then its OWN collapsed gate (never for an
    expand click, whose control is that very header)."""
    nodes = [ds] + ancestor_chain(ds)
    for node in reversed(nodes):                     # outermost first
        if getattr(node, "closed", False):
            return ("closed", node)
        if node is not ds and getattr(node, "expanded", True) is False:
            return ("collapsed", node)
    if header and getattr(ds, "expanded", True) is False and not _header_grabbable(ds):
        return ("collapsed", ds)
    left, top, right, bottom = _visible_rect(ds)
    if right - left <= 0 or bottom - top <= 0:
        return ("outside", ds)
    x, y = press_point
    if not (left <= x <= right and top <= y <= bottom):
        return ("clipped", ds)                        # the press point is clipped
    front = _front_window_at(x, y)
    if front is not None and front is not ds and front not in _window_chain(ds) \
            and front is not _window_of(ds):
        return ("obscured", front)
    return None


def _scroll_container(ds):
    """The nearest ancestor that scrolls (a scroll range, or a scroll offset
    field), else the target's window."""
    for node in ancestor_chain(ds):
        if (getattr(node, "_max_scroll_y", 0) or 0) > 0:
            return node
        if getattr(node, "scroll_offset", None) not in (None, (0, 0)):
            return node
    return _window_of(ds)


def _escape_delta(rect, point, margin):
    """The smallest (dx, dy) moving `rect` so it no longer contains
    `point` (with `margin`); the negation moves the POINT out of the rect."""
    left, top, right, bottom = rect
    x, y = point
    options = [(x + margin - left, 0.0), (x - margin - right, 0.0),
               (0.0, y + margin - top), (0.0, y - margin - bottom)]
    return min(options, key=lambda d: abs(d[0]) + abs(d[1]))


def _escape_rect_delta(rect, target, margin):
    """The smallest (dx, dy) moving `rect` so it no longer overlaps the
    whole `target` rect (plus `margin`) — a move uncovers the CONTROL, not
    just one point on it: a point-sized escape left a 12 px sliver of the
    header free and the next re-check found the rest still covered, the
    windows nudged back and forth and the task gave up (Lukas 09-01: "drag
    windows a little further")."""
    left, top, right, bottom = rect
    t_left, t_top, t_right, t_bottom = target
    options = [(t_right + margin - left, 0.0), (t_left - margin - right, 0.0),
               (0.0, t_bottom + margin - top), (0.0, t_top - margin - bottom)]
    return min(options, key=lambda d: abs(d[0]) + abs(d[1]))


def _fix_cost(name, subject_ds, ds):
    costs = Toggles.Orchestrator.fix_costs
    cost = costs.get(name, 99)
    if name == "move" and subject_ds is _window_of(ds):
        cost += costs.get("anchor_window_penalty", 1)   # the cues' frame moves with it
    return cost


# ── fix mechanisms (generators; each yields once) ───────────────────────

def _header_rect(window_ds):
    """The strip a header press may land in (re-picks for a window subject
    stay inside it — pressing the body would not raise-by-header / move):
    the header between the chevron margin and the buttons margin. Read
    off the window's VISIBLE rect (rect ∩ clip), never its raw width: a
    collapsed window's `width` read 224 while 100 px of header were drawn
    and hittable, so the "safe" centre landed in empty space right of it
    (Lukas 09-01)."""
    left, top, right, _bottom = _visible_rect(window_ds)
    width = max(0.0, right - left)
    safe_left = min(Toggles.Orchestrator.header_safe_left_px, max(4.0, width / 2.0))
    safe_right = max(safe_left + 1.0, width - Toggles.Orchestrator.header_safe_right_px)
    return (left + safe_left, top + 2.0, left + safe_right,
            top + Toggles.Orchestrator.header_height_px)


def _header_grabbable(window_ds):
    """Whether the window's visible header has ROOM for a press between
    the chevron margin and the buttons margin. A collapsed window shrunk
    to its buttons has none — a header press there hits a button or
    nothing, so for a header gesture it counts as a collapsed gate: expand
    it first (Lukas 09-01, the move on a collapsed Loras)."""
    left, _top, right, _bottom = _visible_rect(window_ds)
    return (right - left) >= (Toggles.Orchestrator.header_safe_left_px
                              + Toggles.Orchestrator.header_safe_right_px)


def _header_point(window_ds, offset):
    """A press point on `window_ds` for a header press (raise / move): the
    CENTER of the header's safe strip. The demonstrated offset is not used
    for position — where that user grabbed THAT window means nothing here:
    its x landed on the collapse chevron once (folding the window instead
    of raising it), its y a pixel above the visible top edge, which sits
    inside abs_top (Lukas 09-01). The demo still proves the mechanism."""
    left, top, right, bottom = _header_rect(window_ds)
    return (left + right) / 2.0, (top + bottom) / 2.0


def _point_of(point):
    """A gesture point is a (x, y) tuple or a CALLABLE returning one — the
    live form is re-read every pump of the approach and once more at the
    press, so a target that moves while the cursor travels (the third
    expand of a nested set: the second's reflow shifts it, 09-01) is
    still hit where it IS, not where it was when the gesture was planned."""
    return point() if callable(point) else point


def _tracking(x, y, anchor):
    """A live point that keeps the offset of (x, y) — a re-picked press spot
    _satisfy settled on — from the live `anchor` point, so a target that
    moves carries the re-pick along."""
    ax, ay = anchor()
    dx, dy = x - ax, y - ay
    return lambda: (anchor()[0] + dx, anchor()[1] + dy)


def _click_at(point, ds=None):
    """A click on `ds` (a header's imgui arrow_button, a flat_button). imgui
    reacts only to an item it SUBMITTED that frame: a button activates on
    the press frame and fires on the release frame, and drops between if
    it misses one — so the target's tile is forced live for the press, the
    frame between and the release (Melty skips the hover invalidate on
    press frames, and a blit-served tile never sees the click while the
    handler's own subscriptions still do: the "flaky" gate clicks, 09-01)."""
    yield from _settle_at(point, ds)
    x, y = _point_of(point)
    _press(x, y, ds)
    yield
    _render_live(ds)
    yield
    _release(x, y, ds)
    yield


def _drag_by(point, dx, dy, ds=None):
    yield from _settle_at(point, ds)
    x, y = _point_of(point)
    _press(x, y, ds)
    yield
    duration = glide_seconds(abs(dx) + abs(dy))
    started = time.monotonic()
    while True:
        t = min(1.0, (time.monotonic() - started) / duration)
        _move(x + dx * t, y + dy * t)
        yield
        if t >= 1.0:
            break
    _release(x + dx, y + dy, ds)
    yield


def _gate_press_rect(point):
    """Where a re-pick may land for a gate's control: the expand chevron is
    a few px wide, so a re-pick must stay ON it — anywhere else on the
    header row is not the control. A press point that is covered inside
    this radius has no re-pick and falls through to raise / move."""
    radius = Toggles.Orchestrator.gate_hit_radius_px
    x, y = point
    return (x - radius, y - radius, x + radius, y + radius)


def _fragment_press_point(fragment, window_ds):
    """The screen point a gate fragment's first press lands on once its
    anchors are rebound to `window_ds` (what _replay_fragment does), or
    None for a fragment without a press."""
    origin = None
    if window_ds is not None:
        left = getattr(window_ds, "abs_left", None)
        top = getattr(window_ds, "abs_top", None)
        if left is not None and top is not None:
            origin = (float(left), float(top))
    for event in getattr(fragment, "events", []) or []:
        if event[1] == "down" and event[2] == "left_mouse":
            x, y = float(event[3]), float(event[4])
            if len(event) > 5 and origin is not None:
                return (x + origin[0], y + origin[1])
            return (x, y)
    return None


def _fix_expand(task, node, ds):
    """Open a collapsed collection by pressing its expand control. The
    control's press point is a SUB-GOAL under the same rules as the target
    (preconditions apply recursively — Lukas 09-01: a window sitting over
    the collapse button is answered by a raise of the gate's window or a
    move of the obscurer BEFORE the click, exactly as an obscurer's own
    covered header is for a move), so a gate the user could not click
    either is opened the way the user would open it."""
    offset = expand_offset(editor=_editor_of(node))
    if offset is not None:
        def live_point():
            left, top, _w, _h = _live_rect(node)
            return left + offset[0], top + offset[1]
        # a click that did not open the gate gets ONE more go after the
        # control's press is re-satisfied (a cover that slid back, a
        # tile that missed the press frame)
        for attempt in range(2):
            x, y = yield from task._satisfy(node, live_point, depth=task._depth + 1,
                                            rect=lambda: _gate_press_rect(live_point()))
            yield from _click_at(_tracking(x, y, live_point), node)
            for _ in range(Toggles.Orchestrator.cue_wait_frames):
                if getattr(node, "expanded", True) is not False:
                    return
                yield
    else:
        fragment = fragment_for_gate(display_name(node))
        if fragment is None:
            raise _Abort(f"'{display_name(node)}' is collapsed and no "
                         f"expand demonstration is recorded")
        window_ds = getattr(node, "parent_window", None)
        if _fragment_press_point(fragment, window_ds) is not None:
            # the fragment replays relative to its window's LIVE origin, so
            # its press point is read live (a move of the window carries
            # it along); there is no re-pick (a zero-size rect) — a covered
            # press point goes straight to the raise / move candidates
            press_fn = lambda: _fragment_press_point(fragment, window_ds)
            yield from task._satisfy(node, press_fn, depth=task._depth + 1,
                                     rect=lambda: press_fn() * 2)
        yield from _replay_fragment(fragment, window_ds)
    for _ in range(Toggles.Orchestrator.cue_wait_frames):
        if getattr(node, "expanded", True) is not False:
            break
        yield


def _fix_raise(task, window_ds, ds):
    offset = raise_offset()
    if offset is None:
        raise _Abort("no raise demonstration recorded — record one (click any "
                     "window's header)")
    # sub-goal: the header row must be hittable (re-picked inside the strip)
    x, y = yield from task._satisfy(window_ds, lambda: _header_point(window_ds, offset),
                                    depth=task._depth + 1,
                                    rect=lambda: _header_rect(window_ds), header=True)
    point = _tracking(x, y, lambda: _header_point(window_ds, offset))
    yield from _click_at(point, window_ds)
    # Verified by STATE: the window is in front at its header. The
    # re-check after a fix only sees the unmet change, and a click that
    # raised some OTHER window got it ok - "raise 'Loras' (ok)" with
    # no raise effect anywhere, and the expand click that followed landed
    # on the window still covering the chevron (Lukas 09-01).
    for _ in range(Toggles.Orchestrator.layout_settle_pumps):
        px, py = _point_of(point)
        if _front_window_at(px, py) is window_ds:
            return
        yield
    raise _Abort(f"'{display_name(window_ds)}' did not come to front")


def _fix_move(task, mover_ds, ds, point, rect=None):
    """Move `mover_ds` (the obscurer, or the target's own window) by the
    smallest delta that uncovers the target's press RECT — the re-pick
    rect when the caller gave one (a gate's chevron zone, a header strip),
    else the target's visible rect — and always at least `point`."""
    offset = move_offset()
    if offset is None:
        raise _Abort("no window-move demonstration recorded — record one (drag "
                     "any window by its header)")
    obscurer = task._unmet[1]
    left, top, width, height = _live_rect(obscurer)
    margin = Toggles.Orchestrator.uncover_margin_px
    # What the move must clear: the subject's WHOLE visible rect, plus any
    # re-pick rect and the point. Clearing only the re-pick box (a gate's
    # ±6 px chevron zone) parked the obscurer a hair outside it, and the
    # next re-pick / layout slop put the chevron back over its edge -
    # "it didn't drag it far enough" (Lukas 09-01). A person drags the
    # window off the thing, not off the pixel.
    target = _visible_rect(ds)
    extra = rect() if callable(rect) else rect
    if extra is not None:
        target = (min(target[0], extra[0]), min(target[1], extra[1]),
                  max(target[2], extra[2]), max(target[3], extra[3]))
    x, y = point
    target = (min(target[0], x), min(target[1], y), max(target[2], x), max(target[3], y))
    dx, dy = _escape_rect_delta((left, top, left + width, top + height), target, margin)
    if mover_ds is not obscurer:
        dx, dy = -dx, -dy                               # the point rides with the window
    # Move by GEOMETRY, like a replayed move - the mover's corner must
    # travel by the delta. The re-check after a fix only sees the unmet
    # CHANGE - and a header press RAISES its window, so a drag whose grab
    # never took still reshuffled the pile ("obscured by A" became
    # "obscured by B"), read as ok, and the solver moved A and B in circles
    # for hundreds of rounds without a window moving an inch (Lukas 09-01).
    # A grab that does not take gets ONE more try after re-settling.
    tolerance = Toggles.Orchestrator.move_tolerance_px
    for attempt in range(2):
        x, y = yield from task._satisfy(mover_ds, lambda: _header_point(mover_ds, offset),
                                        depth=task._depth + 1,
                                        rect=lambda: _header_rect(mover_ds), header=True)
        before = _live_rect(mover_ds)[:2]
        yield from _drag_by(_tracking(x, y, lambda: _header_point(mover_ds, offset)),
                            dx, dy, mover_ds)
        yield from _wait_layout(mover_ds)
        after = _live_rect(mover_ds)[:2]
        moved = (after[0] - before[0], after[1] - before[1])
        if abs(moved[0] - dx) <= tolerance and abs(moved[1] - dy) <= tolerance:
            return
        if abs(moved[0]) + abs(moved[1]) > tolerance:
            return                          # it moved, short of the delta: the re-check s
    raise _Abort(f"'{display_name(mover_ds)}' did not move (header drag by "
                 f"({dx:.0f}, {dy:.0f}) not taken)")


def _fix_scroll(task, ds, point_fn):
    container = _scroll_container(ds)
    cl, ct, cr, cb = _visible_rect(container)
    if cr - cl <= 0 or cb - ct <= 0:
        cl, ct, cw, ch = _live_rect(container)
        cr, cb = cl + cw, ct + ch
    cx, cy = (cl + cr) / 2.0, (ct + cb) / 2.0
    # the wheel scrolls to whatever is under the cursor: the viewport's
    # centre is a sub-goal too (a window over it is raised / moved, or a
    # re-pick inside the target's visible rect finds an open spot)
    cx, cy = yield from task._satisfy(container, lambda: (cx, cy), depth=task._depth + 1,
                                      rect=(cl, ct, cr, cb))
    _left, top, _w, height = _live_rect(ds)
    # wheel +1 moves content DOWN (reveals the top) - see the wrapper's
    # scroll block in core_render; a target below the viewport needs
    # negative wheel motion. Which side it is on: the target's own clip
    # rect is rect ∩ viewport, so its bottom sits ABOVE the target's
    # middle exactly when the target is below the viewport.
    clip = getattr(ds, "abs_clip_rect", None)
    clip_bottom = clip[3] if clip is not None else cy
    direction = -1.0 if clip_bottom < top + height / 2.0 else 1.0
    yield from _settle_at((cx, cy))
    for _ in range(Toggles.Orchestrator.scroll_attempts):
        Orchestrator._inject((0.0, "change", "scroll_y", direction))
        yield
        yield
        if (_first_unmet(ds, point_fn()) or (None,))[0] != "outside":
            return


def _candidates(task, unmet, ds, point_fn, rect=None):
    """[(cost, label, generator)] for one unmet precondition, cheapest
    first. A fix whose mechanism is not recorded is listed too — running
    it aborts naming the missing demonstration — unless a cheaper recorded
    one exists, so the abort names the FIRST thing worth recording."""
    kind, node = unmet
    point = point_fn()
    out = []
    if kind == "closed":
        raise _Abort(f"window '{display_name(node)}' is closed")
    if kind == "collapsed":
        out.append((_fix_cost("expand", node, ds), f"expand '{display_name(node)}'",
                    lambda: _fix_expand(task, node, ds)))
    elif kind in ("outside", "clipped"):
        if kind == "clipped":
            out.append((_fix_cost("re_pick", ds, ds), "re-pick the press point",
                        lambda: task._re_pick(ds, rect)))
        out.append((_fix_cost("scroll", node, ds),
                    f"scroll '{display_name(_scroll_container(ds))}'",
                    lambda: _fix_scroll(task, ds, point_fn)))
    elif kind == "obscured":
        window = _window_of(ds)
        out.append((_fix_cost("re_pick", ds, ds), "re-pick the press point",
                    lambda: task._re_pick(ds, rect)))
        out.append((_fix_cost("raise", window, ds), f"raise '{display_name(window)}'",
                    lambda: _fix_raise(task, window, ds)))
        for mover in (node, window):
            if getattr(mover, "closable", True) is False:
                continue
            out.append((_fix_cost("move", mover, ds), f"move '{display_name(mover)}'",
                        lambda m=mover: _fix_move(task, m, ds, point, rect)))
    out.sort(key=lambda c: c[0])
    return [c for c in out if c[0] <= Toggles.Orchestrator.max_disturbance]


def _live_change_on(ds, since_frame):
    """Newest edit-stack Change targeting `ds` recorded at/after
    `since_frame`. A held drag COALESCES — the same Change's `new` advances
    per frame — which is what makes mid-gesture reading possible."""
    for change in reversed(UndoManager.stack.history):
        if change.frame < since_frame:
            break
        if change.draw_state is ds:
            return change
    return None


# ── injection shorthand (absolute coordinates) ───────────────────────────

def _wait_layout(ds, since_frame=None):
    """After a fix moved things (an expand reflowed the rows below it, a
    scroll, a window move): wait until `ds` has been laid out again — its
    wrapper ran after `since_frame` (draw_state.last_seen) — and its rect
    held still for two consecutive frames, so the press point is read
    from LIVE geometry. Read on the frame right after an expand, a leaf
    still carried its pre-expand rect and the retargeted click landed on
    the collection header above it (09-01). Bounded by layout_settle_pumps."""
    last_rect = None
    for _ in range(Toggles.Orchestrator.layout_settle_pumps):
        rect = _live_rect(ds)
        seen = getattr(ds, "last_seen", None)
        laid_out = (since_frame is None or seen is None
                    or (isinstance(seen, (int, float)) and seen > since_frame))
        if laid_out and rect == last_rect:
            return
        last_rect = rect
        yield


def _wait_seconds(seconds):
    """Hold for a recorded pause (wall clock; yields frames meanwhile)."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        yield


def glide_seconds(distance):
    """How long a synthesized move of `distance` px takes: a human hand's
    speed (Toggles.Orchestrator.glide_px_per_second), clamped."""
    return max(Toggles.Orchestrator.glide_min_s,
               min(Toggles.Orchestrator.glide_max_s,
                   distance / max(1.0, Toggles.Orchestrator.glide_px_per_second)))


def _glide(start_x, start_y, point):
    """Eased, WALL-CLOCK paced travel from (start_x, start_y) to `point`:
    one move per pump along a smoothstep curve until glide_seconds elapse.
    `point` may be live (see _point_of): the curve re-aims at the target's
    CURRENT position every pump, so it lands on a target that moved."""
    x, y = _point_of(point)
    distance = ((x - start_x) ** 2 + (y - start_y) ** 2) ** 0.5
    if distance < 0.5:
        _move(x, y)
        yield
        return
    duration = glide_seconds(distance)
    started = time.monotonic()
    while True:
        x, y = _point_of(point)
        fraction = min(1.0, (time.monotonic() - started) / duration)
        eased = fraction * fraction * (3.0 - 2.0 * fraction)
        _move(start_x + (x - start_x) * eased, start_y + (y - start_y) * eased)
        yield
        if fraction >= 1.0:
            break


def _settle_at(point, ds=None):
    """Move the virtual cursor to (x, y) and wait until a press there lands
    on what is under it: the engine's settle rule (Orchestrator.press_ready
    — hover registered by a render after stamp_io saw the move, no real
    button held), AND the target's own tile reporting hover
    (`_bounding_hovered`, set only when its wrapper RUNS — i.e. the tile has
    left the blit cache and its imgui widget is being submitted live; a
    press on a cached tile reaches only the enclosing window's move handle).
    Bounded wait: a view that never reports hover (no bounding tracking)
    proceeds after SETTLE_HOVER_PUMPS."""
    # Glide from wherever the virtual cursor is to (x, y) over several
    # pumps (eased) rather than teleporting: the real pointer travels, hover
    # edges fire naturally along the way, and the progress is visible.
    start_x, start_y = Orchestrator._virtual_x, Orchestrator._virtual_y
    if not Orchestrator._cursor_settled:
        start_x, start_y = _point_of(point)      # first move of the run, no origin to glide from
    yield from _glide(start_x, start_y, point)
    while True:
        x, y = _point_of(point)
        if abs(x - Orchestrator._virtual_x) + abs(y - Orchestrator._virtual_y) > 0.5:
            _move(x, y)                          # the target moved since the glide landed
            yield
            continue
        if Orchestrator.press_ready((x, y)):
            break
        yield
    waited = 0
    while (ds is not None and not getattr(ds, "_bounding_hovered", False)
           and waited < Toggles.Orchestrator.settle_hover_pumps):
        waited += 1
        yield


# change_value's own injections bypass the engine's continuous-mouse layer
# (glide=False): every move here is already paced (_glide, see servo) and
# every press already settled. Routed through that layer, an eased step
# over its teleport threshold spawned a SECONDARY glide and the press /
# release queued behind it: the press landed late, and a glide running
# while a button was virtually held dragged the cursor by the header.
def _press(x, y, ds=None):
    """Press — with the target's tile forced LIVE on the press frame: imgui
    only activates an item it submitted the frame it saw the click, and a
    press frame otherwise serves the tile from the blit cache (the hover
    invalidate is suppressed on press frames, see apply_move_to_front).
    This is also what makes a press on a NON-front window work — the
    press raises it and the raise reshuffles tiles that frame — so
    front-ness is deliberately not a precondition: the Orchestrator's own
    window (Play was just pressed in it) is in front of every target, and
    a "must be front" gate could never be met (Lukas 09-01)."""
    _render_live(ds)
    Orchestrator._inject((0.0, "down", "left_mouse", x, y), glide=False)


def _render_live(ds):
    """Dirty the target's tile so the coming frame submits its imgui item."""
    invalidate = getattr(ds, "invalidate", None) if ds is not None else None
    if callable(invalidate):
        try:
            invalidate()
        except Exception:
            pass


def _move(x, y):
    Orchestrator._inject((0.0, "move", x, y), glide=False)


def _release(x, y, ds=None):
    _render_live(ds)                             # a button FIRES on the release frame
    Orchestrator._inject((0.0, "up", "left_mouse", x, y), glide=False)


def _key(key, mods=0):
    Orchestrator._inject((0.0, "key", key, mods))


def _char(codepoint):
    Orchestrator._inject((0.0, "char", codepoint))


def _leaf_point(ds, take, task=None):
    """Press point: the caller's explicit point when it has one (a replay
    remap presses where the tape recorded — the cursor is already there),
    else the take's demonstrated press as a FRACTION of its leaf's rect,
    re-applied to THIS leaf's live rect (the lora_dropout generalization).
    No take / no press -> the leaf's center."""
    if task is not None and task.press_point is not None:
        return task.press_point
    frac = None
    if task is not None and getattr(task, "press_frac", None) is not None:
        frac = task.press_frac                   # the cue's OWN press, first
    if frac is None and take is not None:
        cue = terminal_cue(take)
        frac = cue_get(cue, "press_frac") if cue is not None else None
    frac = frac or (0.5, 0.5)
    left, top, width, height = _live_rect(ds)
    return (left + frac[0] * width, top + frac[1] * height)


def _replay_fragment(fragment, window_ds):
    """Replay a gate fragment with its anchors rebound to `window_ds`'s
    live origin — the recorded window-relative geometry lands in the
    TARGET window. Runs at 2x the configured replay speed (gates are means,
    not the demonstration)."""
    origin = None
    if window_ds is not None:
        left = getattr(window_ds, "abs_left", None)
        top = getattr(window_ds, "abs_top", None)
        if left is not None and top is not None:
            origin = (left, top)
    speed = max(0.05, Toggles.Orchestrator.replay_speed) * 2.0
    t0 = time.monotonic()
    for event in fragment.events:
        while event[0] > (time.monotonic() - t0) * speed:
            yield
        _inject_rebased(event, origin)
    # the fragment's events may have been QUEUED FOR a continuous-mouse
    # glide (a reused take injects far from the cursor): hold until they
    # have actually landed, so the caller's wait for the effect starts
    # after the click, not before it
    while Orchestrator._glide_queue:
        yield
    yield


def _inject_rebased(event, origin):
    kind = event[1]
    if origin is not None and kind in ("move", "down", "up"):
        slot = 2 if kind == "move" else 3
        if len(event) > slot + 2:                   # relativized: rebase to origin
            Orchestrator._inject(event[:slot] + (event[slot] + origin[0],
                                                 event[slot + 1] + origin[1]))
            return
    Orchestrator._inject(event)


# ── the task ─────────────────────────────────────────────────────────────

class ValueTask:
    """One change_value run: a generator stepped per frame by the engine.
    error is None on success; result is the verified final value."""

    def __init__(self, path, to, within=None, universe=None, eps=None):
        self.path = path
        self.to = to
        self.pacing = {}                 # predefined pauses to honour (a replay remap sets these)
        self.press_point = None          # explicit press point (a replay remap: the recorded press)
        self.gates_only = False          # replay without override: open the gates where the tape presses
        self.within = within
        self.universe = universe
        self.eps = eps
        self.error = None
        self.result = None
        self.start_frame = 0
        self.trace_target = None
        self._verify_eps = 0.0
        self._skip_verify = False
        self._generator = None
        self._done = threading.Event()
        self._take = None
        self._unmet = None
        self._depth = 0
        self._picked = None
        # Fix labels running up the sub-goal chain. A fix's own sub-goal
        # never re-offers the fix: raising W needs W's header hittable,
        # and with the wrong tests the cheapest candidate for THAT was
        # "raise W" again - seven levels of the same raise until max_depth
        # stopped them, for every window in the tree, before the fix that
        # actually helps was ever tried at depth 1 (Lukas 09-01).
        self._active_fixes = []
        self.attempts = []               # "fix (result)" log - the abort message lists it
        self._last_fix_frame = None      # Melty.frame_count when the last fix finished (layout wait)
        # A precondition-only run (the orchestrator window's per-precondition
        # run chip): satisfy preconditions up to and including the one with
        # this key (see precondition.py), then stop - no value change, no
        # verify. Outer preconditions along the way are satisfied too: the
        # solver is outermost-first, so "run the third one" performs the
        # first two, exactly as the full command would.
        self.until = None
        self.orchestration = None        # the take this run belongs to (failure / success rows)
        # The command's press target: "leaf" (a value editor, the press at
        # the demonstrated fraction of its rect) or "header" (a WINDOW's
        # header strip - the move archetype; `to` is then the (dx, dy) the
        # window must travel). The precondition ladder is the same either
        # way; only the press point and the re-pick rect differ.
        self.gesture = "leaf"
        # The press as a FRACTION of the target's rect, from the cue that
        # demonstrated it - the press point for an editor with no archetype
        # (any widget: the cue still knows where on the target it pressed).
        self.press_frac = None
        # The press as an OFFSET from the target's live top-left (px) - a
        # gates-only replay presses where the recording pressed on the
        # target, wherever the target sits now (a reorder moved the target,
        # a scroll fix shifted it). Overrides press_frac if set.
        self.press_offset = None
        # The CONTROL the cue pressed, as (width, height, frac_x, frac_y)
        # around the press point: where a re-pick may land. A header
        # button is a 30 px square on a 4000 px collection: a re-pick
        # anywhere else on the collection is not the button. None = the
        # target's visible rect.
        self.control = None

    def __str__(self):
        if self.until is not None:
            return f"precondition {self.until[0]} '{self.until[1]}' for {format_path(parse(self.path))}"
        if self.gates_only:
            return f"open gates for {format_path(parse(self.path))}"
        if self.gesture == "header":
            return f"move_window({format_path(parse(self.path))} by {self.to!r})"
        return f"change_value({format_path(parse(self.path))} → {self.to!r})"

    def _press_point_fn(self, ds, take=None):
        """The press point this task aims at, live: the explicit point when
        one was given (a replay remap: the recorded press, re-anchored),
        else the header centre for a window gesture, else the demonstrated
        fraction of the leaf's rect."""
        if self.gesture == "header":
            # NEVER the cue's press offset here (see _header_point): a raise
            # recorded off a body click carries an offset deep in the
            # window, and a collapsed window is 32 px tall — the point read
            # as "clipped" on every round (Lukas 09-01, the stress test)
            return lambda: (self.press_point if self.press_point is not None
                            else _header_point(ds, move_offset()))
        if self.press_offset is not None:
            def from_offset():
                if self.press_point is not None:
                    return self.press_point
                left, top, _width, _height = _live_rect(ds)
                return (left + self.press_offset[0], top + self.press_offset[1])
            return from_offset
        if self.press_frac is not None:
            def from_frac():
                if self.press_point is not None:
                    return self.press_point
                left, top, width, height = _live_rect(ds)
                return (left + self.press_frac[0] * width, top + self.press_frac[1] * height)
            return from_frac
        return lambda: _leaf_point(ds, take, self)

    def _press_rect_fn(self, ds):
        """The rect a re-pick may land in: the header strip for a window
        gesture, the leaf's visible rect otherwise (None = default)."""
        if self.gesture == "header":
            return lambda: _header_rect(ds)
        if self.control is not None:
            point_fn = self._press_point_fn(ds)

            def control_rect():
                width, height, frac_x, frac_y = self.control
                x, y = point_fn()
                left, top = x - frac_x * width, y - frac_y * height
                return (left, top, left + width, top + height)
            return control_rect
        return None

    def fail(self, message):
        self.error = self.error or str(message)
        self._done.set()

    def finish(self):
        self._done.set()

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.error is None

    # ----- generator body (engine steps it; yield = wait one frame) ----

    def run(self):
        try:
            yield from self._run()
        except _PreconditionDone:
            self.result = "satisfied"
        except _Abort as abort:
            self.error = str(abort)

    def _run(self):
        ds = yield from self._resolve_structural()
        if self._last_fix_frame is not None:
            yield from _wait_layout(ds, since_frame=self._last_fix_frame)
        if self.until is not None:
            # the chosen precondition was structural and is now open (or was
            # never unmet); the geometric ones still run but the ladder is
            # identical to the command's own - _apply_fixes stops the run
            # the moment the chosen one is satisfied
            take = take_for(_ARCHETYPE_BY_EDITOR.get(_editor_of(ds)))
            yield from self._satisfy(ds, self._press_point_fn(ds, take),
                                     rect=self._press_rect_fn(ds),
                                     header=self.gesture == "header")
            self.result = "satisfied"
            self._skip_verify = True
            return
        if self.gesture == "header":
            yield from self._run_move(ds)
            return
        if self.gates_only:
            # a recorded gesture about to play verbatim: make its target
            # hittable at the RECORDED press point (collapsed parents opened
            # by _resolve_structural; here the geometric ones - scrolled
            # out, covered), then hand back to the tape
            yield from self._satisfy(ds, self._press_point_fn(ds), rect=self._press_rect_fn(ds))
            self.result = ds
            return
        editor = _editor_of(ds)
        archetype = _ARCHETYPE_BY_EDITOR.get(editor)
        if archetype is None:
            raise _Abort(f"no archetype for editor {editor!r}")
        take = take_for(archetype)
        if take is None and archetype in _NEEDS_TAKE:
            raise _Abort(f"no {archetype} demonstration recorded — record one "
                         f"({'drag any float field' if archetype == 'drag' else 'edit any text field'})")
        self._take = take
        # Geometric preconditions at the press point the executor will use
        # (a re-pick fix moves task.press_point; _leaf_point honours it).
        yield from self._satisfy(ds, lambda: _leaf_point(ds, take, self))
        self.trace_target = ds                     # engine trace reads its hover/active state
        runner = {"drag": _run_drag, "text": _run_text, "toggle": _run_toggle}[archetype]
        yield from runner(self, ds, take)
        if not self._skip_verify:
            yield from self._verify(ds)

    def _run_move(self, window):
        """The move archetype: grab the window by its header (the press a
        sub-goal like any other — a window over the header is raised /
        moved first, a re-pick stays inside the strip), drag by `to` =
        (dx, dy) at the recorded pace, verify by GEOMETRY: the window's
        live corner moved by the delta (within move_tolerance_px — a hard
        display limit or a collision that stopped it short is an honest
        abort naming how far it got)."""
        if move_offset() is None and not self.gates_only:
            raise _Abort("no window-move demonstration recorded — record one (drag "
                         "any window by its header)")
        if self.gates_only:
            dx = dy = 0.0                    # the tape does the moving; only the press matters here
        else:
            try:
                dx, dy = float(self.to[0]), float(self.to[1])
            except (TypeError, ValueError, IndexError):
                raise _Abort(f"move needs a (dx, dy) delta, got {self.to!r}")
        point_fn = self._press_point_fn(window)
        yield from self._wait_pacing("before_press")
        x, y = yield from self._satisfy(window, point_fn, rect=self._press_rect_fn(window),
                                        header=True)
        header = lambda: _header_point(window, move_offset())
        before = _live_rect(window)[:2]
        if self.gates_only:
            self.result = window                 # the tape presses and drags back
            return
        yield from _drag_by(_tracking(x, y, header), dx, dy, window)
        tolerance = Toggles.Orchestrator.move_tolerance_px
        moved = (0.0, 0.0)
        for _ in range(Toggles.Orchestrator.cue_wait_frames):
            after = _live_rect(window)[:2]
            moved = (after[0] - before[0], after[1] - before[1])
            if abs(moved[0] - dx) <= tolerance and abs(moved[1] - dy) <= tolerance:
                self.result = after
                self._skip_verify = True
                return
            yield
        raise _Abort(f"window '{display_name(window)}' moved by ({moved[0]:.0f}, {moved[1]:.0f}), "
                     f"wanted ({dx:.0f}, {dy:.0f}) — stopped short (display edge / collision?)")

    def _wait_pacing(self, key):
        yield from _wait_seconds(self.pacing.get(key, 0.0))

    def _resolve_structural(self):
        """Resolve the path and open the STRUCTURAL gates (closed windows,
        collapsed ancestors) until the leaf is live with geometry — the
        geometric preconditions (viewport, cover) need the press point,
        which needs the take, which needs the resolved editor."""
        # one gate opens per pass, so the bound is on STALLS - the same gate
        # unmet for consecutive passes (it re-closed, or the fix missed) -
        # never on the number of gates; three nested collections take three
        # passes, not three failures (09-01). The upper cap only stops a
        # pathological chain.
        stalls, last_gate = 0, None
        for _pass in range(max(64, Toggles.Orchestrator.gate_attempts * 4)):
            missing = None
            try:
                ds, _suffix = resolve_recorded(self.path, within=self.within,
                                               universe=self.universe)
            except NoMatch as error:
                ds, missing = None, error
            except Ambiguous as ambiguous:
                raise _Abort(str(ambiguous))
            if ds is not None and _reachable(ds):
                return ds
            frontier = ds
            if frontier is None:
                segments = parse(self.path)
                for length in range(len(segments) - 1, 0, -1):
                    try:
                        frontier = resolve(segments[:length], within=self.within,
                                           universe=self.universe)
                        break
                    except (NoMatch, Ambiguous):
                        continue
            unmet = _first_unmet(frontier, _leaf_point(frontier, None)) if frontier is not None else None
            if unmet is None or unmet[0] not in ("closed", "collapsed"):
                if missing is not None:
                    raise _Abort(str(missing))         # the NoMatch, default "unreachable"
                raise _Abort(f"{format_path(parse(self.path))} resolved but is not "
                             f"hittable, and no closed gate found on its path")
            gate = (unmet[0], id(unmet[1]))
            stalls = stalls + 1 if gate == last_gate else 0
            last_gate = gate
            if stalls >= max(1, Toggles.Orchestrator.gate_attempts):
                raise _Abort(f"{display_name(unmet[1])} kept {'closing' if unmet[0] == 'closed' else 'collapsing'} "
                             f"— gave up after {Toggles.Orchestrator.gate_attempts} attempts")
            yield from self._apply_fixes(unmet, frontier,
                                         lambda f=frontier: _leaf_point(f, None))
            yield                                    # a layout frame before re-resolving
        raise _Abort("gates kept closing — gave up after "
                     f"{max(64, Toggles.Orchestrator.gate_attempts * 4)} passes")

    def _satisfy(self, ds, point_fn, depth=0, rect=None, header=False):
        """Make `ds` hittable at the press point `point_fn()` returns — a
        CALLABLE, re-evaluated after every fix, because fixes move things
        (a scroll shifts the target, a move shifts a header); a re-pick
        replaces it with a constant inside `rect` (default the view's
        visible rect). Finds the first unmet precondition, runs its
        candidate fixes cheapest-first, re-checks after each. Returns the
        final point. Used for the target and, recursively, for a fix's own
        press point (the obscurer's header can itself be covered — a
        sub-goal under the same rules, depth-limited)."""
        if depth > Toggles.Orchestrator.fix_depth:
            raise _Abort("precondition fixes nested too deep")
        saved_depth, self._depth = self._depth, depth
        since = len(self.attempts)            # this scope's own attempts (the message lists just these)
        try:
            for _round in range(max(1, Toggles.Orchestrator.gate_attempts) * 2):
                unmet = _first_unmet(ds, point_fn(), header=header)
                if unmet is None:
                    break
                point_fn = yield from self._apply_fixes(unmet, ds, point_fn, rect,
                                                        since=since, header=header)
                yield
            point = point_fn()
            unmet = _first_unmet(ds, point, header=header)
            if unmet is not None:
                raise _Abort(self._unmet_message(unmet, ds, since=since))
            if depth == 0:
                self.press_point = point
            return point
        finally:
            self._depth = saved_depth

    def _apply_fixes(self, unmet, ds, point_fn, rect=None, since=0, header=False):
        """Run the candidates for one unmet precondition until the
        precondition changes (fixed, or a different one surfaced). Returns
        the press point callable (a re-pick replaces it). `since` = where
        this scope's attempts start in the flat log."""
        self._unmet = unmet
        for cost, label, make in _candidates(self, unmet, ds, point_fn, rect):
            if label in self._active_fixes:
                continue                      # this fix's own sub-goal: not a candidate
            self._picked = None
            self._active_fixes.append(label)
            try:
                yield from make()
            except _Abort as abort:
                # a nested failure's own attempts are already in the flat
                # log; keep its HEAD here, never its transcript (re-embedding
                # it at every level blew the report past a megabyte, 09-01)
                self.attempts.append(f"{label} ({_abort_head(abort)})")
                continue
            finally:
                self._active_fixes.remove(label)
                self._unmet = unmet           # a nested fix overwrote it
            self._last_fix_frame = Melty.frame_count
            if self._picked is not None:
                point_fn = (lambda p=self._picked: p)
            else:
                yield from _wait_layout(ds)          # the fix moved things: re-checking geometry
            after = _first_unmet(ds, point_fn(), header=header)
            if after is None or after != unmet:
                self.attempts.append(f"{label} (ok, cost {cost})")
                if self.until is not None and precondition_key(unmet) == self.until:
                    raise _PreconditionDone()
                return point_fn
            self.attempts.append(f"{label} (still {unmet[0]})")
        raise _Abort(self._unmet_message(unmet, ds, since=since))

    def _re_pick(self, ds, rect=None):
        """The zero-cost fix: another press point inside the visible part
        of the target (or `rect`) that is NOT covered — a partially
        obscured target needs no window touched."""
        if callable(rect):
            rect = rect()                # a live rect follows the fixes that moved things
        left, top, right, bottom = rect if rect is not None else _visible_rect(ds)
        window = _window_of(ds)
        for fx, fy in ((0.5, 0.5), (0.2, 0.5), (0.8, 0.5), (0.5, 0.2), (0.5, 0.8),
                       (0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)):
            x = left + fx * (right - left)
            y = top + fy * (bottom - top)
            front = _front_window_at(x, y)
            if front is None or front is window:
                self._picked = (x, y)
                return
            yield
        raise _Abort("no uncovered point on the target")

    def _unmet_message(self, unmet, ds, since=0):
        kind, node = unmet
        what = {"closed": f"window '{display_name(node)}' is closed",
                "collapsed": f"'{display_name(node)}' is collapsed",
                "outside": f"'{display_name(ds)}' is outside its scroll viewport",
                "clipped": f"'{display_name(ds)}' is clipped at its press point",
                "obscured": f"'{display_name(ds)}' is covered by '{display_name(node)}'"}[kind]
        attempts = self.attempts[since:]
        tried = "; ".join(attempts) if attempts else "nothing applicable"
        return f"{what} — tried: {tried}"

    def _verify(self, ds):
        """The parameterized cue: a Change on the TARGET whose new value is
        `to` — expectation from the caller, mechanism from the take."""
        change = None
        for _ in range(Toggles.Orchestrator.cue_wait_frames):
            change = _live_change_on(ds, self.start_frame)
            if change is not None and _value_matches(change.new, self.to, self._verify_eps):
                self.result = change.new
                return
            yield
        reached = change.new if change is not None else "unchanged"
        raise _Abort(f"value is {reached!r}, wanted {self.to!r}")


def _abort_head(abort):
    """A precondition failure's one-line cause, without its attempts."""
    return str(abort).split(" — tried:")[0]


def _value_matches(value, target, eps):
    if isinstance(target, bool) or isinstance(target, str):
        return value == target
    if isinstance(target, (int, float)) and isinstance(value, (int, float)):
        return abs(float(value) - float(target)) <= max(eps, 1e-9)
    return value == target


# ── preconditions as data (the orchestrator window lists and runs them) ──

def precondition_key(unmet):
    """The identity of an unmet precondition across frames: its kind and
    the display name of the node the fix acts on — what the window's run
    chip hands back as ValueTask.until."""
    kind, node = unmet
    return (kind, display_name(node))


def list_preconditions(path, within=None, universe=None, gesture="leaf", press_frac=None):
    """Every precondition currently unmet on the way to `path`'s target,
    outermost first, as rows the orchestrator window renders:
    {"key", "kind", "node", "label", "fixes": [(cost, label), …]} — the
    same walk the solver takes (structural gates on the resolved chain or
    on the deepest resolvable frontier, then the first geometric one at
    the demonstrated press point), without running anything. Empty when
    the target is hittable now; a NoMatch / Ambiguous path yields one
    "unresolved" row."""
    task = ValueTask(path, None, within=within, universe=universe)
    task.gesture = gesture
    task.press_frac = press_frac
    try:
        ds, _suffix = resolve_recorded(path, within=within, universe=universe)
    except (NoMatch, Ambiguous) as error:
        ds = None
        frontier = None
        for length in range(len(parse(path)) - 1, 0, -1):
            try:
                frontier = resolve(parse(path)[:length], within=within, universe=universe)
                break
            except (NoMatch, Ambiguous):
                continue
        if frontier is None:
            return [{"key": ("unresolved", format_path(parse(path))), "kind": "unresolved",
                     "node": format_path(parse(path)), "label": str(error), "fixes": []}]
    else:
        frontier = ds
    rows = []
    nodes = [frontier] + ancestor_chain(frontier)
    for node in reversed(nodes):                     # outermost first, like _first_unmet
        if getattr(node, "closed", False):
            rows.append(("closed", node))
        elif node is not frontier and getattr(node, "expanded", True) is False:
            rows.append(("collapsed", node))
    if ds is not None and not rows and _reachable(ds):
        take = take_for(_ARCHETYPE_BY_EDITOR.get(_editor_of(ds)))
        point = task._press_point_fn(ds, take)()
        unmet = _first_unmet(ds, point, header=gesture == "header")
        if unmet is not None:
            rows.append(unmet)
    elif ds is None and not rows:
        rows.append(("unresolved", frontier))
    out = []
    for unmet in rows:
        kind, node = unmet
        fixes = []
        if kind == "unresolved":
            out.append({"key": precondition_key(unmet), "kind": kind, "node": display_name(node),
                        "label": f"{format_path(parse(path))} does not resolve below "
                                 f"'{display_name(node)}' (open / expand it first)",
                        "fixes": []})
            continue
        if kind != "unresolved":
            try:
                subject = ds if ds is not None else node
                point_fn = task._press_point_fn(subject)
                fixes = [(cost, label) for cost, label, _make
                         in _candidates(task, unmet, subject, point_fn,
                                        task._press_rect_fn(subject))]
            except _Abort as abort:
                fixes = [(99, str(abort))]
        out.append({"key": precondition_key(unmet), "kind": kind,
                    "node": display_name(node),
                    "label": task._unmet_message(unmet, ds if ds is not None else node)
                    .split(" — tried:")[0],
                    "fixes": fixes})
    return out


def describe_target(path, within=None, universe=None, gesture="leaf", press_frac=None):
    """What the solver sees for `path` right now — the facts every
    precondition verdict is computed from, for the window's hittable row
    and the failure report: the resolved leaf (name, editor, rect, visible
    rect, window), the press point it would use, and the front window at
    that point. Never raises."""
    out = {"path": format_path(parse(path))}
    try:
        ds, _suffix = resolve_recorded(path, within=within, universe=universe)
    except (NoMatch, Ambiguous) as error:
        out["resolved"] = None
        out["error"] = str(error)
        return out
    out["resolved"] = full_name(ds)
    out["editor"] = _editor_of(ds)
    out["rect"] = tuple(round(v, 1) for v in _live_rect(ds))
    out["visible"] = tuple(round(v, 1) for v in _visible_rect(ds))
    out["reachable"] = _reachable(ds)
    window = _window_of(ds)
    out["window"] = display_name(window) if window is not None else None
    out["chain"] = [display_name(w) for w in _window_chain(ds)]
    take = take_for(_ARCHETYPE_BY_EDITOR.get(_editor_of(ds)))
    out["take"] = getattr(take, "name", None) if take is not None else None
    probe = ValueTask(path, None, within=within, universe=universe)
    probe.gesture = gesture
    probe.press_frac = press_frac
    point = probe._press_point_fn(ds, take)()
    out["point"] = (round(point[0], 1), round(point[1], 1))
    front = _front_window_at(point[0], point[1])
    out["front"] = display_name(front) if front is not None else None
    unmet = _first_unmet(ds, point, header=gesture == "header")
    out["unmet"] = (unmet[0], display_name(unmet[1])) if unmet is not None else None
    return out


def precondition_task(path, key, orchestration=None, within=None, universe=None,
                      gesture="leaf", press_frac=None):
    """A ValueTask that satisfies preconditions up to and including `key`
    (from list_preconditions) and stops — the window's run chip. Submit it
    to the Orchestrator like any task."""
    task = ValueTask(path, None, within=within, universe=universe)
    task.until = tuple(key)
    task.orchestration = orchestration
    task.gesture = gesture
    task.press_frac = press_frac
    return task


def move_window(path, delta, within=None, universe=None):
    """Move the window `path` names by `delta` = (dx, dy) through a real
    header drag — the move archetype (see ValueTask._run_move). The header
    press is a precondition sub-goal like any leaf press."""
    task = ValueTask(path, (float(delta[0]), float(delta[1])), within=within, universe=universe)
    task.gesture = "header"
    return Orchestrator.submit(task)


# ── archetype executors ───────────────────────────────────────────────────

def _read_value(ds, since_frame):
    """The target's live numeric value: the coalescing Change's `new` when
    one has been recorded this run, else the draw_state's own held value
    (`_raw_input_value` — updated the frame the wrapper runs, so it sees an
    edit the undo record missed or lagged). None when neither is numeric."""
    change = _live_change_on(ds, since_frame)
    if change is not None and isinstance(change.new, (int, float)):
        return float(change.new)
    value = _current_value(ds)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _run_drag(task, ds, take):
    """Press at the demonstrated fraction, confirm the widget took the press
    (imgui active), probe to measure the LIVE gain, secant-servo to the
    target, release. The recorded gain is never used — the probe replaces
    all calibration. The sensor is the live value (_read_value): undo
    Change first, the draw_state's held value as the fallback."""
    probe_px = Toggles.Orchestrator.servo_probe_px
    max_step = Toggles.Orchestrator.servo_max_step_px
    point = lambda: _leaf_point(ds, take, task)   # live: the leaf may shift while we travel
    since = task.start_frame
    yield from _settle_at(point, ds)
    yield from _wait_seconds(task.pacing.get("before_press", 0.0))
    x, y = point()
    baseline = _read_value(ds, since)
    _press(x, y, ds)
    yield
    # the widget must be ACTIVE before the probe moves - a press whose frame
    # served the tile from cache never reaches imgui (active flickers off);
    # keep the tile live while waiting (a raise on the press frame
    # reshuffles tiles, and imgui drops an active item it misses that frame)
    for _ in range(Toggles.Orchestrator.settle_hover_pumps):
        if getattr(ds, "_imgui_is_active", False):
            break
        _render_live(ds)
        yield
    else:
        _release(x, y, ds)
        yield
        raise _Abort("press did not activate the widget (tile not live at the press?)")
    yield from _wait_seconds(task.pacing.get("before_drag", 0.0))
    if baseline is None:
        _release(x, y, ds)
        raise _Abort("target holds no numeric value to drag")
    # Probe TOWARD the target: a value pinned at a min/max limit ignores a
    # push further into the clamp (alpha at its 99.264 ceiling ate a +8 px
    # probe), while a push toward the target always has room.
    direction = -1.0 if float(task.to) < baseline else 1.0
    # The probe GROWS until the field responds: an integer drag accumulates
    # fractional units (rank at 16 px per unit) and ignores a short probe, so
    # 8 px is only the first try - double up to servo_max_step_px.
    origin_x = x
    step = probe_px * direction
    current = baseline
    while abs(step) <= max_step:
        x = origin_x + step
        _move(x, y)
        yield
        yield                                        # a frame for the value to settle
        current = _read_value(ds, since)
        if current is not None and current != baseline:
            break
        step *= 2.0
    else:
        step = origin_x - x                          # nothing moved, measure the reverse from here
    if current is None or current == baseline:
        # nothing toward the target: the other way separates pinned-at-the-limit
        # (the target's beyond the range) from not-draggable-at-all
        x = origin_x - (x - origin_x)
        _move(x, y)
        yield
        yield
        reverse = _read_value(ds, since)
        _release(x, y, ds)
        yield
        if reverse is not None and reverse != baseline:
            raise _Abort(f"value {baseline:g} is at its limit in the direction of "
                         f"{float(task.to):g} — target outside the field's range")
        raise _Abort(f"probe drag changed nothing (value {baseline!r}, widget active="
                     f"{bool(getattr(ds, '_imgui_is_active', False))}) — not a draggable value?")
    gain = (current - baseline) / step
    # convergence tolerance within half a pixel's worth of value (the widget's own
    # tolerance), unless the caller pinned one
    eps = task.eps if task.eps is not None else max(abs(gain) * 0.5, 1e-9)
    task._verify_eps = eps
    # ---- easing travel: spread the estimated distance over the RECORDED
    # drag_duration on a smooth curve (a hand, not a lurch), refining the
    # distance estimate from the value as it comes in; the step servo below
    # then only closes the last quantum ----
    duration = task.pacing.get("drag_duration", 0.0)
    if duration > 0.0 and gain != 0.0:
        travel = (float(task.to) - baseline) / gain          # px from the press, first estimate
        start_x = x                                          # the curve begins where the probe left off
        started = time.monotonic()
        heading = 1.0 if travel >= 0 else -1.0
        while True:
            fraction = min(1.0, (time.monotonic() - started) / duration)
            eased = fraction * fraction * (3.0 - 2.0 * fraction)
            goal = start_x + (origin_x + travel - start_x) * eased
            # MONOTONIC: the endpoint estimate may shrink as the gain refines,
            # but the cursor never reverses mid-drag (a quantized field's
            # noisy secant had it fighting itself)
            if (goal - x) * heading > 0:
                x = goal
                _move(x, y)
            yield
            fresh = _read_value(ds, since)
            # whole-drag secant: in once the cursor is a probe-length past
            # the press (nearer, the ratio is noisy; one tick at the origin
            # once divided by 1e-9 and pinned the travel at zero); the
            # endpoint moves at most a quarter of the way per tick (damped)
            if (fresh is not None and fresh != baseline
                    and abs(x - origin_x) >= probe_px):
                measured = (fresh - baseline) / (x - origin_x)
                current = fresh
                if measured != 0.0 and (measured > 0) == (gain > 0):
                    gain = gain + (measured - gain) * 0.25
                    travel = (float(task.to) - baseline) / gain
            if fraction >= 1.0:
                break
        yield
        yield
        fresh = _read_value(ds, since)
        if fresh is not None:
            current = fresh
    stalls = 0
    damping = 1.0
    last_residual = None
    for _ in range(Toggles.Orchestrator.servo_max_steps):
        residual = float(task.to) - current
        if abs(residual) <= eps:
            break
        dx = max(-max_step, min(max_step, residual / gain))
        # damped: overshooting flips the residual's sign - halve the stride
        # from then on so the close never ping-pongs across the target
        if last_residual is not None and (residual > 0) != (last_residual > 0):
            damping *= 0.5
        last_residual = residual
        dx *= damping
        x += dx
        _move(x, y)
        # A move lands imgui at the NEXT frame's io stamp and the Change
        # lands after that frame's render: read two pumps later (the probe
        # already did; the loop read one pump early and called every step a
        # stall).
        yield
        yield
        fresh = _read_value(ds, since)
        if fresh is None or fresh == current:
            stalls += 1
            if stalls >= 3:                          # three settled reads, no motion
                _release(x, y, ds)
                yield
                raise _Abort(f"stalled at {current:g} heading for {float(task.to):g} "
                             f"(min/max clamp?)")
            yield                                    # give a next frame one more chance
            continue
        stalls = 0
        gain = (fresh - current) / dx                # re-measure every step
        current = fresh
    else:
        _release(x, y, ds)
        yield
        raise _Abort(f"did not converge (at {current:g}, wanted {float(task.to):g})")
    _release(x, y, ds)
    yield


def _run_text(task, ds, take):
    """Click to focus, synthesized clear, payload typed from `to` in the
    event shape a real keystroke produces, one clear-and-retype retry."""
    text = str(task.to)
    point = lambda: _leaf_point(ds, take, task)
    since = task.start_frame
    yield from _settle_at(point, ds)
    x, y = point()
    _press(x, y, ds)
    yield
    _release(x, y, ds)
    yield
    for attempt in range(2):
        # ---- clear: select-all + delete; End+Backspace fallback ----
        _key(glfw.KEY_A, glfw.MOD_CONTROL)
        yield
        _key(glfw.KEY_DELETE)
        yield
        yield
        remaining = _current_value(ds)
        if isinstance(remaining, str) and remaining:
            _key(glfw.KEY_END)
            yield
            for _ in range(len(remaining)):
                _key(glfw.KEY_BACKSPACE)
            yield
            yield
        # ---- payload: key + char per character, like a real keystroke ----
        for index, character in enumerate(text):
            keycode = _keycode_for(character)
            if keycode is not None:
                _key(keycode, glfw.MOD_SHIFT if character.isupper() else 0)
            _char(ord(character))
            if index % 4 == 3:
                yield                                # pace: 4 chars a frame
        yield
        yield
        change = _live_change_on(ds, since)
        if change is not None and change.new == text:
            return                                   # _verify confirms exactly
        if attempt == 0:
            continue                                 # once more: clear + retype
    # fall through - _verify delivers an honest comparison with what we reached


def _keycode_for(character):
    """Best-effort glfw keycode for an ASCII character (letters, digits,
    space). Anything else rides the char event alone."""
    if character.isascii() and character.isalpha():
        return ord(character.upper())
    if character.isdigit():
        return ord(character)
    if character == " ":
        return glfw.KEY_SPACE
    return None


def _run_toggle(task, ds, take):
    if isinstance(task.to, bool) and _current_value(ds) == task.to:
        task.result = task.to                        # nothing to do, nothing to verify
        task._skip_verify = True
        return
    point = lambda: _leaf_point(ds, take, task)
    yield from _settle_at(point, ds)
    x, y = point()
    _press(x, y, ds)
    yield
    _release(x, y, ds)
    yield