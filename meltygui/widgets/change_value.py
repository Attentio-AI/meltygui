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

Preconditions are one universal guard: the resolved target must be
REACHABLE (BVH membership, with a structural fallback: geometry present and
no collapsed/closed ancestor). Unreachable → walk the containment path for
the closed gate, open a collapsed view by replaying the recorded expand
take rebound to the target's window (real clicks — the integration-testing
point), re-resolve, retry up to Toggles.Orchestrator.gate_attempts. A
closed WINDOW or a missing demonstration aborts with the unmet need named.

Execution: a ValueTask generator stepped once per frame by
Orchestrator.pump (real input muted, Esc aborts, same contract as replay).
`task = change_value(...)` returns immediately; task.wait() blocks for test
harnesses that drive frames themselves.
"""
import threading
import time

import glfw

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_views.core_undo import UndoManager
from src.lsd.gl_gui.view.playground.orchestrator import Orchestrator, cue_get
from src.lsd.gl_gui.view.playground.selectors import (
    resolve, parse, format_path, ancestor_chain, display_name, NoMatch, Ambiguous)

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


def fragment_for_gate():
    """A take that opens a collapsed view. Preferred: a take whose last
    meaningful cue is an "expand" EFFECT (the header arrow's ledger entry).
    Legacy: a bool flipping True on a NON-leaf (recordings from when
    expansion was assumed to reach the undo stack)."""
    for take in orchestration_store().values():
        for cue in reversed(getattr(take, "cues", []) or []):
            kind = cue_get(cue, "kind")
            if kind == "expand":
                return take
            if kind == "Change":
                if (cue_get(cue, "value_type") == "bool"
                        and cue_get(cue, "new") in (True, "True")
                        and cue_get(cue, "editor") not in _ARCHETYPE_BY_EDITOR):
                    return take
                break                       # a leaf-edit take, not an expand take
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
    """The one universal guard: is this target actually hittable? BVH
    membership when the index is populated, UNIONED with the structural
    fallback (geometry present, no collapsed/closed ancestor) — the BVH
    only holds views that registered hit boxes, so absence alone is not
    proof of unreachability."""
    bvh = getattr(Melty, "_bvh_id_to_ds", None)
    if bvh and any(candidate is ds for candidate in bvh.values()):
        return True
    left, top, width, height = _live_rect(ds)
    if width <= 0 or height <= 0:
        return False
    for node in [ds] + ancestor_chain(ds):
        if getattr(node, "closed", False):
            return False
        if node is not ds and getattr(node, "expanded", True) is False:
            return False
    return True


def _blocked_gate(path, ds, within, universe):
    """The OUTERMOST closed gate on the way to `path`: ("closed"|"expanded",
    node). With no resolved ds (a never-rendered leaf), the deepest
    resolvable prefix of the path is the frontier."""
    nodes = []
    if ds is not None:
        nodes = [ds] + ancestor_chain(ds)
    else:
        segments = parse(path)
        for length in range(len(segments) - 1, 0, -1):
            try:
                frontier = resolve(segments[:length], within=within, universe=universe)
            except (NoMatch, Ambiguous):
                continue
            nodes = [frontier] + ancestor_chain(frontier)
            break
    for node in reversed(nodes):                     # outermost gate first
        if getattr(node, "closed", False):
            return ("closed", node)
        if getattr(node, "expanded", True) is False:
            return ("expanded", node)
    return None


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

def _press(x, y):
    Orchestrator._inject((0.0, "down", "left_mouse", x, y))


def _move(x, y):
    Orchestrator._inject((0.0, "move", x, y))


def _release(x, y):
    Orchestrator._inject((0.0, "up", "left_mouse", x, y))


def _key(key, mods=0):
    Orchestrator._inject((0.0, "key", key, mods))


def _char(codepoint):
    Orchestrator._inject((0.0, "char", codepoint))


def _leaf_point(ds, take):
    """Press point: the take's demonstrated press as a FRACTION of its
    leaf's rect, re-applied to THIS leaf's live rect (the lora_dropout
    generalization). No take / no press -> the leaf's center."""
    frac = None
    if take is not None:
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
        self.within = within
        self.universe = universe
        self.eps = eps
        self.error = None
        self.result = None
        self.start_frame = 0
        self._verify_eps = 0.0
        self._skip_verify = False
        self._generator = None
        self._done = threading.Event()

    def __str__(self):
        return f"change_value({format_path(parse(self.path))} → {self.to!r})"

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
        except _Abort as abort:
            self.error = str(abort)

    def _run(self):
        ds = yield from self._resolve_with_gates()
        editor = _editor_of(ds)
        archetype = _ARCHETYPE_BY_EDITOR.get(editor)
        if archetype is None:
            raise _Abort(f"no archetype for editor {editor!r}")
        take = take_for(archetype)
        if take is None and archetype in _NEEDS_TAKE:
            raise _Abort(f"no {archetype} demonstration recorded — record one "
                         f"({'drag any float field' if archetype == 'drag' else 'edit any text field'})")
        runner = {"drag": _run_drag, "text": _run_text, "toggle": _run_toggle}[archetype]
        yield from runner(self, ds, take)
        if not self._skip_verify:
            yield from self._verify(ds)

    def _resolve_with_gates(self):
        for _attempt in range(max(1, Toggles.Orchestrator.gate_attempts)):
            try:
                ds = resolve(self.path, within=self.within, universe=self.universe)
            except NoMatch:
                ds = None
            except Ambiguous as ambiguous:
                raise _Abort(str(ambiguous))
            if ds is not None and _reachable(ds):
                return ds
            gate = _blocked_gate(self.path, ds, self.within, self.universe)
            if gate is None:
                raise _Abort("target unreachable and no closed gate found on its path")
            gate_kind, gate_ds = gate
            if gate_kind == "closed":
                raise _Abort(f"window '{display_name(gate_ds)}' is closed")
            fragment = fragment_for_gate()
            if fragment is None:
                raise _Abort(f"'{display_name(gate_ds)}' is collapsed and no "
                             f"expand demonstration is recorded")
            yield from _replay_fragment(fragment,
                                        getattr(gate_ds, "parent_window", None))
            for _ in range(Toggles.Orchestrator.cue_wait_frames):
                if getattr(gate_ds, "expanded", True) is not False:
                    break
                yield
            else:
                raise _Abort(f"expand replay did not open '{display_name(gate_ds)}'")
            yield                                    # one layout frame before re-resolving
        raise _Abort("gates kept closing — gave up "
                     f"after {Toggles.Orchestrator.gate_attempts} attempts")

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


def playable_commands(orchestration):
    """The take's parameterized program: [(cue_index, path, value)] — one
    entry per leaf-edit cue, in order, value = the user's override (keyed
    str(cue_index) in orchestration.overrides) else the recorded one. Path
    is the cue's FULL recorded chain (maximal capture — the most specific
    address; resolve complains with candidates if it's ambiguous)."""
    from src.lsd.gl_gui.view.playground.orchestrator import cue_get, _COMMAND_VERBS
    overrides = getattr(orchestration, "overrides", None) or {}
    commands = []
    for index, cue in enumerate(getattr(orchestration, "cues", None) or []):
        if cue_get(cue, "kind") != "Change" \
                or cue_get(cue, "editor") not in _COMMAND_VERBS:
            continue
        chain = cue_get(cue, "chain") or []
        path = tuple(chain) if chain else (cue_get(cue, "name") or "?",)
        value = overrides.get(str(index), cue_get(cue, "new", cue_get(cue, "new_repr")))
        commands.append((index, path, value))
    return commands


class CommandPlayTask:
    """Generalized playback: run a take as its COMMAND list — one
    change_value per leaf-edit cue, recorded value unless overridden — so
    edited arguments replay through the real archetype executors (servo,
    text, gates) instead of the raw event tape. `command_cursor` is the cue
    index currently executing (the window's Commands tab highlights it).
    Same task contract as ValueTask (Orchestrator.submit steps run())."""

    def __init__(self, orchestration, universe=None, only=None):
        self.orchestration = orchestration
        self.universe = universe
        self.only = only                 # cue index: run just this one command
        self.error = None
        self.result = None
        self.start_frame = 0
        self.command_cursor = None
        self._generator = None
        self._done = threading.Event()

    def __str__(self):
        return f"play '{getattr(self.orchestration, 'name', '?')}' (args)"

    def fail(self, message):
        self.error = self.error or str(message)
        self._done.set()

    def finish(self):
        self._done.set()

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.error is None

    def run(self):
        try:
            yield from self._run()
        except _Abort as abort:
            self.error = str(abort)

    def _run(self):
        commands = playable_commands(self.orchestration)
        if self.only is not None:
            commands = [c for c in commands if c[0] == self.only]
        if not commands:
            raise _Abort("no parameterized commands in this take")
        done = 0
        for cue_index, path, value in commands:
            self.command_cursor = cue_index
            inner = ValueTask(path, value, universe=self.universe)
            inner.start_frame = Melty.frame_count
            yield from inner.run()
            if inner.error:
                raise _Abort(f"{format_path(parse(path))}: {inner.error}")
            done += 1
        self.command_cursor = None
        self.result = done


def _value_matches(value, target, eps):
    if isinstance(target, bool) or isinstance(target, str):
        return value == target
    if isinstance(target, (int, float)) and isinstance(value, (int, float)):
        return abs(float(value) - float(target)) <= max(eps, 1e-9)
    return value == target


# ── archetype executors ───────────────────────────────────────────────────

def _run_drag(task, ds, take):
    """Press at the demonstrated fraction, probe to measure the LIVE gain,
    secant-servo to the target, release. The recorded gain is never used —
    the probe replaces all calibration."""
    probe_px = Toggles.Orchestrator.servo_probe_px
    max_step = Toggles.Orchestrator.servo_max_step_px
    x, y = _leaf_point(ds, take)
    since = task.start_frame
    _press(x, y)
    yield
    x += probe_px
    _move(x, y)
    yield
    yield                                            # a frame for the Change to land
    change = _live_change_on(ds, since)
    if change is None or not isinstance(change.new, (int, float)):
        _release(x, y)
        raise _Abort("probe drag changed nothing — not a draggable value?")
    current = float(change.new)
    gain = (current - float(change.old)) / probe_px
    if gain == 0.0:                                  # probed INTO a clamp - try the other way
        x -= 2 * probe_px
        _move(x, y)
        yield
        yield
        fresh = float(_live_change_on(ds, since).new)
        if fresh == current:
            _release(x, y)
            raise _Abort("value did not respond to the probe in either direction")
        gain = (fresh - current) / (-2 * probe_px)
        current = fresh
    # convergence tolerance within half a pixel's worth of value (the widget's own
    # tolerance), unless the caller pinned one
    eps = task.eps if task.eps is not None else max(abs(gain) * 0.5, 1e-9)
    task._verify_eps = eps
    for _ in range(Toggles.Orchestrator.servo_max_steps):
        residual = float(task.to) - current
        if abs(residual) <= eps:
            break
        dx = max(-max_step, min(max_step, residual / gain))
        x += dx
        _move(x, y)
        yield
        fresh = float(_live_change_on(ds, since).new)
        if fresh == current:
            _release(x, y)
            yield
            raise _Abort(f"stalled at {current:g} heading for {float(task.to):g} "
                         f"(min/max clamp?)")
        gain = (fresh - current) / dx                # re-measure every step
        current = fresh
    else:
        _release(x, y)
        yield
        raise _Abort(f"did not converge (at {current:g}, wanted {float(task.to):g})")
    _release(x, y)
    yield


def _run_text(task, ds, take):
    """Click to focus, synthesized clear, payload typed from `to` in the
    event shape a real keystroke produces, one clear-and-retype retry."""
    text = str(task.to)
    x, y = _leaf_point(ds, take)
    since = task.start_frame
    _press(x, y)
    yield
    _release(x, y)
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
    x, y = _leaf_point(ds, take)
    _press(x, y)
    yield
    _release(x, y)
    yield
