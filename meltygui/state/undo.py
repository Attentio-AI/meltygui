import collections
import time
from enum import Enum

import imgui

from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header


class Change:
    """One recorded edit (or coalesced burst): a draw_state's value moving from
    `old` to `new`. `ui` is a snapshot of the draw_state's transient UI state
    (caret/selection/scroll) captured before the edit, restored alongside `old`
    on undo. While a burst keeps coalescing, `new`/`t`/`ui_after` advance but
    `old`/`ui`/`direction` stay pinned to the group's start, so one undo reverts
    the whole burst and drops the caret where it began. `ui_after` is the caret
    snapshot at the group's current end, used by redo so re-applying lands the
    caret after the redone text. `t` is the wall-clock time of the last edit
    folded in; `direction` is 'insert'/'delete'/'replace'/None (None for non-text
    values)."""

    def __init__(self, draw_state, old, new, ui=None, t=0.0, direction=None, ui_after=None,
                 group_id=0, frame=0):
        self.draw_state = draw_state
        self.old = old
        self.new = new
        self.ui = ui
        self.ui_after = ui_after
        self.t = t
        self.direction = direction
        # Undo group: changes from one user action (e.g. multiple views reacting to
        # the same edit, recorded within a frame or two) share a group_id and are
        # undone/redone together. `frame` is the frame_count the change last
        # touched, used to decide group membership.
        self.group_id = group_id
        self.frame = frame
    #
    # def __repr__(self):
    #     name = getattr(self.draw_state, "name", "?")
    #     return f"Change({name}: {self.old!r} -> {self.new!r})"





@window(view_func=RenderFuncs.draw_undo_manager, live=True)
class UndoManager:
    # draw_state -> ordered list of Changes recorded for that node. Safe to key
    # on the draw_state object: DrawState uses identity equality and hashes on
    # its unique id, so distinct nodes never collide as keys.
    change_history = {}
    MAX_HISTORY = 20

    # Only record a change when both old and new are one of these immutable
    # primitives. Snapshotting a mutable object by reference is unsound - it
    # could be aliased and mutated after the fact, so undo would restore the
    # wrong value. Start with types that are safe to keep by reference; widen
    # as snapshotting for richer types is implemented.
    APPROVED_TYPES = (float, int, str, bool, tuple, Enum, RelaxedEnum)

    # Global timeline of every Change in the order it happened. This is the
    # companion that bounds total size: when this grows past MAX_HISTORY the
    # oldest Change is dropped from here and from its per-node list above.
    history = collections.deque(maxlen=MAX_HISTORY)

    # Redo timeline: undo() moves the popped Change here; redo() moves it back to
    # `history` and re-applies its `new`. Any fresh user action (a record() that
    # isn't an undo/redo restore) clears this - you can't redo after diverging.
    redo_stack = collections.deque(maxlen=MAX_HISTORY)

    # Undo coalescing: consecutive edits to the same draw_state fold into the last
    # group instead of adding a new entry, so undo reverts a whole burst at once
    # rather than one character at a time. A group is committed (a fresh entry
    # starts) on any of: a pause longer than COALESCE_WINDOW seconds, a different
    # draw_state, a non-contiguous edit (the new edit doesn't start where the group
    # end - e.g. after an undo or a cursor jump), a direction flip between
    # inserting and deleting, or a word break (a non-space typed right after a
    # space/newline). Non-text values (floats/ints) coalesce on time + contiguity
    # only: a drag becomes one undo.
    COALESCE_WINDOW = 0.6

    # Cross-view grouping: one user action can make several different views record
    # a change in the same frame (or a frame or two apart, when a derived view
    # updates a tick later). Changes whose frames are within GROUP_FRAME_WINDOW of
    # the previous change join the same group and undo/redo as a unit, so the user
    # doesn't see edits alternating back and forth between views. (Same-draw_state
    # bursts still fold in _can_coalesce regardless of frame distance.)
    GROUP_FRAME_WINDOW = 2
    _next_group_id = 0

    settle_for = 2 # 2 frame at start

    @classmethod
    def _request(cls, ds, value, ui):
        # Register a (value, ui) request with Melty for `ds`. next_render
        # intercepts that draw_state's return next frame and reports (True, value)
        # with the caret/selection restored. Its parent then writes `value` back
        # into the model, exactly as if the user had typed it.
        Core.melty.undo_requests[ds] = (value, ui)
        # Force the target and its parent wrapper to re-render this frame so the
        # restored data actually propagates: a blitted parent would otherwise never
        # call the child wrapper that performs the interception.
        cache = getattr(Core.melty, "cache", None)
        if cache is not None:
            cache.invalidate_up(ds._tile_id, force=True)
            if ds._parent is not None:
                cache.invalidate_up(ds._parent._tile_id, force=True)
        request_render()

    @classmethod
    def _pop_group(cls):
        """Pop the newest group (all trailing changes sharing the top group_id)
        off `history`, newest-first."""
        if not cls.history:
            return []
        gid = cls.history[-1].group_id
        group = []
        while cls.history and cls.history[-1].group_id == gid:
            group.append(cls.history.pop())
        return group

    @classmethod
    def undo(cls):
        # Move the newest group to the redo stack and restore every change in
        # it (pre-edit value + caret), so all views from one action revert at once.
        group = cls._pop_group()
        if not group:
            return
        cls.redo_stack.append(group)
        for change in group:
            cls._request(change.draw_state, change.old, change.ui)

    @classmethod
    def redo(cls):
        # Move the most-recently-undone group back to history and re-apply every
        # change in it (post-edit value + caret).
        if not cls.redo_stack:
            return
        group = cls.redo_stack.pop()
        for change in reversed(group):   # preserve original append order
            cls.history.append(change)
        for change in group:
            cls._request(change.draw_state, change.new, change.ui_after)

    @classmethod
    def _can_coalesce(cls, last, draw_state, old, new, now, direction):
        """Whether this edit (old -> new) should fold into `last` instead of
        starting a new group. `direction` is the edit kind for text (None
        otherwise)."""
        if last is None:
            return False
        if last.draw_state is not draw_state:
            return False
        if now - last.t > cls.COALESCE_WINDOW:          # pause -> commit group
            return False

        if direction is None:
            # Non-text (numbers/tuples): fold only a continuous gesture - a held
            # widget streams a value every frame. Exact value-contiguity is the
            # wrong test here: a drag widget streams float32-precision values
            # (3.828000068664551) that read back next frame as a clean 3.828, so
            # `last.new == old` fails every frame and each frame becomes its own
            # undo step. The reliable "continuous gesture" signal is the widget being
            # actively dragged; discrete clicks/toggles aren't active, so they
            # become separate undo steps.
            return bool(getattr(draw_state, "_imgui_is_active", False))

        # Text: exact value-contiguity is meaningful (detects undo / cursor jump).
        if last.new != old:
            return False
        if last.direction != direction:                 # insert<->delete flip
            return False
        if direction == "insert" and _starts_new_word(old, new):
            return False                                # word boundary -> commit group
        return True

    @classmethod
    def record(cls, draw_state, old, new):
        if Core.melty.frame_count < cls.settle_for:
            return
        if not (isinstance(old, cls.APPROVED_TYPES)
                and isinstance(new, cls.APPROVED_TYPES)):
            return
        # Skip no- change. Several renderers (draw_text, draw_collection, the
        # @window source views) report changed=True every frame with old == new.
        # Logging those floods the bounded history deque and evicts the real
        # edits, so undo ends up restoring an identical value (a visible no-op).
        # An undo entry where nothing changed is pointless by definition.
        try:
            if old == new:
                return
        except Exception:
            pass

        # A genuine new edit (only origins reach here; undo/redo restores bypass
        # record().) diverges from any undone changes - drop redo.
        cls.redo_stack.clear()

        now = time.time()
        frame = Core.melty.frame_count
        direction = _edit_direction(old, new) if (isinstance(old, str) and isinstance(new, str)) else None
        # Post-edit caret: record() runs in the wrapper frame (after the body), so
        # the draw_state's caret now reflects the result of this edit. Redo needs it.
        ui_after = draw_state.capture_undo_state() if hasattr(draw_state, "capture_undo_state") else None

        # Burst fold: find the most recent change for this draw_state still inside
        # the coalesce window. Searching back (not just history[-1]) is what lets a
        # burst keep folding even when another view's change landed on top between
        # keystrokes in the alternating-views case.
        target = None
        for c in reversed(cls.history):
            if now - c.t > cls.COALESCE_WINDOW:
                break                                    # older than window; deque is time-ordered
            if c.draw_state is draw_state:
                target = c
                break
        if target is not None and cls._can_coalesce(target, draw_state, old, new, now, direction):
            target.new = new
            target.t = now
            target.ui_after = ui_after
            target.frame = frame
            return

        # New change. Join the previous change's group when they're within
        # GROUP_FRAME_WINDOW frames (same user action / cascade) - unless that
        # group already holds a change for this draw_state, in which case this is a
        # fresh semantic step (e.g. a new word boundary) and gets its own group.
        gid = None
        last = cls.history[-1] if cls.history else None
        if last is not None and (frame - last.frame) <= cls.GROUP_FRAME_WINDOW:
            gid = last.group_id
            for c in reversed(cls.history):
                if c.group_id != gid:
                    break
                if c.draw_state is draw_state:
                    gid = None                           # ds already in this group -> new group
                    break
        if gid is None:
            cls._next_group_id += 1
            gid = cls._next_group_id

        change = Change(draw_state, old, new, ui=getattr(draw_state, "_undo_pre", None),
                        t=now, direction=direction, ui_after=ui_after, group_id=gid, frame=frame)
        cls.history.append(change)

        # while len(cls.history) > cls.MAX_HISTORY:
        #     evicted = cls.history.popleft()
        #     per_node = cls.change_history.get(evicted.draw_state)
        #     if per_node:
        #         per_node.remove(evicted)
        #         if not per_node:
        #             del cls.change_history[evicted.draw_state]


def _edit_direction(old, new):
    """Classify a text edit by length: 'insert' grew, 'delete' shrank, 'replace'
    kept the same length (e.g. overwriting a selection)."""
    if len(new) > len(old):
        return "insert"
    if len(new) < len(old):
        return "delete"
    return "replace"


def _diff_span(old, new):
    """Minimal differing span as (prefix_len, inserted_text). Strips the common
    prefix and suffix so `inserted` is the run that `new` adds over `old`."""
    p = 0
    m = min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    s = 0
    while s < (m - p) and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return p, new[p:len(new) - s]


def _starts_new_word(old, new):
    """True when this insert begins a new word — a non-space inserted immediately
    after a space/newline — which commits the current group so each word is its
    own undo step (typing 'hello world ' -> two groups)."""
    p, inserted = _diff_span(old, new)
    if not inserted or inserted[0].isspace():
        return False
    char_before = old[p - 1] if p > 0 else ""
    return char_before != "" and char_before.isspace()


# The primitive value editors - the genuine leaves of the render tree. Their
# changes originate from direct user interaction (not bubbled up from a child),
# so they're always valid undo origins. Needed because imgui status flags aren't
# reliable across widget types: drag_float sets is_item_edited but checkbox does
# not, so a flag-only origin check silently dropped every boolean toggle.
LEAF_EDITOR_FUNCS = frozenset({"draw_bool", "draw_str", "draw_float", "draw_int", "draw_text"})


def _is_leaf_editor(draw_state):
    return getattr(getattr(draw_state, "_view_func", None), "__name__", None) in LEAF_EDITOR_FUNCS


def handle_undo(changed, old_value, new_value, draw_state):
    if not changed:
        return
    # Only the widget the user directly interacted with may record an undo entry.
    # A single edit bubbles up through every render_func chain - the focused text
    # editor (draw_text) and its converter/wrapper ancestors (code_to_io_wrapped,
    # collections) all report the same string change, often on different frames
    # because conversion runs async. Those pass-through draw_states must never
    # become undo-stack elements; only the interaction origin may: a primitive
    # value editor, the focused text editor, or the imgui-active widget this frame.
    # A container/converter is none of these.
    is_origin = (_is_leaf_editor(draw_state)
                 or draw_state is Core.melty.text_focused_ds
                 or getattr(draw_state, "_imgui_is_edited", False))
    if not is_origin:
        return
    UndoManager.record(draw_state, old_value, new_value)


