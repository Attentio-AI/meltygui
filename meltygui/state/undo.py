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

    @property
    def display_name(self):
        return getattr(self.draw_state, "name", None) or "?"

    def apply(self, undo):
        """Re-apply one side of this change (undo → `old`, redo → `new`).
        Subclasses override this — it's the ONLY kind-specific hook, so
        UndoStack never inspects change types."""
        if undo:
            UndoManager._request(self.draw_state, self.old, self.ui)
        else:
            UndoManager._request(self.draw_state, self.new, self.ui_after)
    #
    # def __repr__(self):
    #     name = getattr(self.draw_state, "name", "?")
    #     return f"Change({name}: {self.old!r} -> {self.new!r})"

class UndoStack:
    """One independent undo/redo timeline. The edit stack (UndoManager.stack)
    and the navigation stack (NavUndo.stack) are instances; adding another
    timeline is: make an UndoStack, push Change subclasses into it, and wire
    something to its undo()/redo(). Changes re-apply THEMSELVES (Change.apply)
    — a stack never inspects change kinds. Grouping (several changes from one
    user action undoing as a unit) rides group_id, same as before the split."""

    def __init__(self, name, maxlen=128):
        self.name = name
        self.history = collections.deque(maxlen=maxlen)
        # undo() moves a popped group here; redo() moves it back. Any fresh
        # push clears this - you can't redo after diverging.
        self.redo_stack = collections.deque(maxlen=maxlen)
        self._next_group_id = 0

    def new_group_id(self):
        self._next_group_id += 1
        return self._next_group_id

    def push(self, change):
        self.redo_stack.clear()
        self.history.append(change)

    def can_undo(self):
        return bool(self.history)

    def can_redo(self):
        return bool(self.redo_stack)

    def _pop_group(self):
        """Pop the newest group (all trailing changes sharing the top group_id)
        off `history`, newest-first."""
        if not self.history:
            return []
        gid = self.history[-1].group_id
        group = []
        while self.history and self.history[-1].group_id == gid:
            group.append(self.history.pop())
        return group

    def undo(self):
        group = self._pop_group()
        if not group:
            return
        self.redo_stack.append(group)
        for change in group:
            change.apply(undo=True)

    def redo(self):
        if not self.redo_stack:
            return
        group = self.redo_stack.pop()
        for change in reversed(group):   # restore original append order
            self.history.append(change)
        for change in group:
            change.apply(undo=False)


class NavChange(Change):
    """A file-navigation step (editor tab switch / jump-to). `old`/`new` are
    (path, line, instance) locations — not values — and there is no
    draw_state: replay navigates (open_in_editor / tab select) instead of
    writing a value back through the wrapper. line None means "wherever that
    file's editor last left its caret" (each file's draw_text keeps its own
    caret/scroll on its persistent draw_state); instance is the code-editor
    window the step happened in, so replay lands in the same window."""

    def __init__(self, old_loc, new_loc, t=0.0, group_id=0, frame=0):
        super().__init__(None, old_loc, new_loc, t=t, group_id=group_id,
                         frame=frame)

    @property
    def display_name(self):
        return "goto"

    def apply(self, undo):
        NavUndo._apply_location(self.old if undo else self.new)


class WindowChange(Change):
    """A window open/close step. `draw_state` is the WINDOW's draw_state;
    `old`/`new` are the `closed` flag before/after the user's toggle. Replay
    just writes the flag back (raising the window when it reopens)."""

    def __init__(self, window_ds, closed_before, closed_after, t=0.0,
                 group_id=0, frame=0):
        super().__init__(window_ds, closed_before, closed_after, t=t,
                         group_id=group_id, frame=frame)

    @property
    def display_name(self):
        base = str(getattr(self.draw_state, "name", "?")).split("##")[0]
        return f"{'close' if self.new else 'open'} {base}"

    def apply(self, undo):
        wds = self.draw_state
        wds.closed = self.old if undo else self.new
        if not wds.closed:
            Core.melty.move_window_to_front(wds)
        # Repaint the window's own subtree (a reopen must redraw its content,
        # a close must clear its blit from the compositor) ...
        if Core.melty.cache is not None and wds._tile_id is not None:
            Core.melty.cache.invalidate_up(wds._tile_id, force=True, max_depth=4)
        # ... and the dock/window list rows, same frame for interactive paths.
        Core.melty.cache.invalidate_up_by_obj(Core.melty.registered_windows)
        request_render()


class CompareChange(Change):
    """A Compare-With selection step in a code-editor window. `old`/`new`
    are the PERSISTED compare tokens ('' none, 'HEAD', 'local', 'latest',
    or a commit id) — the moving 'latest' token replays as 'latest', not a
    frozen hash. No draw_state: replay posts a model command
    (OpenFiles.compare_request) that the target editor instance's body
    adopts next frame (externals never write draw_states directly)."""

    def __init__(self, instance, old_token, new_token, t=0.0, group_id=0,
                 frame=0):
        super().__init__(None, old_token, new_token, t=t, group_id=group_id,
                         frame=frame)
        self.instance = instance

    @property
    def display_name(self):
        return f"compare {(self.new or 'none')[:12]}"

    def apply(self, undo):
        root = getattr(Core.melty.vis, "root", None)
        open_files = getattr(root, "open_files", None)
        if open_files is None:
            return
        open_files.compare_request = (self.instance,
                                      self.old if undo else self.new)
        from src.lsd.gl_gui.view.playground.open_files import editor_window_draw_state
        win = editor_window_draw_state(self.instance)
        if win is not None:
            win.closed = False
            Core.melty.move_window_to_front(win)
            if Core.melty.cache is not None and win._tile_id is not None:
                Core.melty.cache.invalidate_up(win._tile_id, force=True,
                                               max_depth=4)
        request_render()


@window(view_func=RenderFuncs.draw_undo_manager, live=True)
class UndoManager:
    # draw_state -> ordered list of Changes recorded for that node. Safe to key
    # on the draw_state object: DrawState uses identity equality and hashes on
    # its unique id, so distinct nodes never collide as keys.
    change_history = {}
    MAX_HISTORY = 128

    # Only record a change when both old and new are one of these immutable
    # primitives. Snapshotting a mutable object by reference is unsound - it
    # could be aliased and mutated after the fact, so undo would restore the
    # wrong value. Start with types that are safe to keep by reference; widen
    # as snapshotting for richer types is implemented.
    APPROVED_TYPES = (float, int, str, bool, tuple, Enum, RelaxedEnum)

    # The EDIT timeline (value changes). Navigation lives on its own stack -
    # NavUndo.stack - so Ctrl+Z never yanks the viewport and Ctrl+Shift+arrows
    # never deletes text. `history`/`redo_stack` alias the stack's deques (same
    # objects) for the render func and older call sites.
    stack = UndoStack("edits", maxlen=MAX_HISTORY)
    history = stack.history
    redo_stack = stack.redo_stack

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
    def undo(cls):
        cls.stack.undo()

    @classmethod
    def redo(cls):
        cls.stack.redo()

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
        # Collection mutations (drag-drop reorders, see drag_drop.py) are
        # frozen "insert x at key y"-style records - safe to hold by
        # reference like the approved primitives, and the whole point of
        # them is not snapshotting the dict they edit. `old` is the
        # inverse mutation, `new` the applied one; undo/redo apply either
        # side to the live collection via the wrapper-tail interception.
        is_mutation = (getattr(old, "__collection_mutation__", False)
                       and getattr(new, "__collection_mutation__", False))
        if not is_mutation and not (isinstance(old, cls.APPROVED_TYPES)
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
            gid = cls.stack.new_group_id()

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


class NavUndo:
    """The navigation timeline — a separate UndoStack from text edits, so
    stepping back through WHERE you were never touches WHAT you typed.
    Records location moves (editor tab switches, jump-tos) and window
    open/close toggles. Driven by Ctrl+Shift+Left/Right (root handler in
    new_core_view) and the Fast Dock back/forward buttons. Gated on
    Toggles.CodeEditor.undo_navigation."""

    stack = UndoStack("navigation")

    # Reentrancy guard: while undo/redo replays, the navigation it triggers
    # (open_in_editor → tab select + jump-to, window closed writes) should not
    # record fresh entries.
    _restoring = False

    @classmethod
    def _recordable(cls):
        from src.lsd.gl_gui.toggles import Toggles
        return (Toggles.CodeEditor.undo_navigation and not cls._restoring
                and Core.melty.frame_count >= UndoManager.settle_for)

    @classmethod
    def record_location(cls, old_loc, new_loc):
        """Push a location step. Locations are (path, line, instance) tuples
        (line may be None; instance is which code-editor window — replay must
        land in the SAME window, not the primary). Each step is its own
        group."""
        if not cls._recordable():
            return
        if old_loc == new_loc:
            return
        # Re-selecting the already-selected file with no target line moves
        # nothing - not worth an undo step. Same file in the OTHER editor
        # instance is a real move (the [2:] slice keeps pre-instance 2-tuples
        # comparing equal).
        if (new_loc[1] is None and old_loc[0] == new_loc[0]
                and old_loc[2:] == new_loc[2:]):
            return
        cls.stack.push(NavChange(old_loc, new_loc, t=time.time(),
                                 group_id=cls.stack.new_group_id(),
                                 frame=Core.melty.frame_count))

    @classmethod
    def record_compare(cls, instance, old_token, new_token):
        """Push a Compare-With selection step (editor dropdown / clear ×)."""
        if not cls._recordable() or old_token == new_token:
            return
        cls.stack.push(CompareChange(instance, old_token, new_token,
                                     t=time.time(),
                                     group_id=cls.stack.new_group_id(),
                                     frame=Core.melty.frame_count))

    @classmethod
    def record_window(cls, window_ds, closed_before, closed_after):
        """Push a window open/close step (the user toggled `closed` — dock row
        click, chrome ×)."""
        if not cls._recordable() or closed_before == closed_after:
            return
        cls.stack.push(WindowChange(window_ds, closed_before, closed_after,
                                    t=time.time(),
                                    group_id=cls.stack.new_group_id(),
                                    frame=Core.melty.frame_count))

    @classmethod
    def _apply_location(cls, loc):
        """Replay one side of a NavChange: select the tab (reopening it if it
        was closed) and land on the recorded line — in the editor INSTANCE
        the step was recorded in (pre-instance 2-tuples default to 0)."""
        loc = tuple(loc or ())
        path = loc[0] if loc else None
        line = loc[1] if len(loc) > 1 else None
        inst = loc[2] if len(loc) > 2 else 0
        if path is None:
            return
        from src.lsd.gl_gui.view.playground.open_files import (
            open_in_editor, editor_window_draw_state)
        from src.lsd.gl_gui.model.app_model import OpenFiles
        if path.startswith(OpenFiles.GIT_DIFF_PREFIX):
            # Pseudo-path - never route through open_in_editor (open_file
            # would spin up a real host for it). Only re-select if the
            # diff tab is still open.
            root = getattr(Core.melty.vis, "root", None)
            open_files = getattr(root, "open_files", None)
            if open_files is not None and path in open_files.open_paths:
                open_files.jump_to_path = path
                # Force the recorded instance - jump_to_instance decides
                # which editor body adopts the pending selection, so a stale
                # value from an earlier jump would hand it to the wrong one.
                open_files.jump_to_instance = inst
                # The summon + past-the-blank invalidate open_in_editor does -
                # the tab selection is adopted inside the editor body.
                win = editor_window_draw_state(inst)
                if win is not None:
                    win.closed = False
                    Core.melty.move_window_to_front(win)
                    if Core.melty.cache is not None and win._tile_id is not None:
                        Core.melty.cache.invalidate_up(win._tile_id, force=True,
                                                       max_depth=4)
                request_render()
            return
        # Replaying into a secondary instance rides the editor_window path of
        # open_in_editor - which assumes the originating window is already
        # front. A replay can't guarantee that, so summon/raise it here first.
        # Instance window missing (never drawn) → fall back to the primary.
        win = editor_window_draw_state(inst) if inst else None
        if win is not None:
            win.closed = False
            Core.melty.move_window_to_front(win)
        open_in_editor(path, line_number=line, editor_window=win)

    @classmethod
    def undo(cls):
        cls._restoring = True
        try:
            cls.stack.undo()
        finally:
            cls._restoring = False

    @classmethod
    def redo(cls):
        cls._restoring = True
        try:
            cls.stack.redo()
        finally:
            cls._restoring = False


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
    prefix and suffix so `inserted` is the run that `new` adds over `old`.

    Chunked: equal 4KB slices skip at C memcmp speed, per-char refinement only
    inside the first mismatching chunk. The original per-char Python walk was
    O(buffer) per FOLDED INSERT — record() runs it via _starts_new_word on
    every consecutive-insert keystroke, and on a ~166KB buffer that was a
    measured ~20ms slice of the edited-frame wrapper epilogue (held-Enter
    bursts; alternating insert/delete never reached it, which is why the cost
    came and went between sessions)."""
    lo, ln = len(old), len(new)
    m = min(lo, ln)
    chunk = 4096
    p = 0
    while p < m:
        step = min(chunk, m - p)
        if old[p:p + step] == new[p:p + step]:
            p += step
            continue
        e = p + step
        while p < e and old[p] == new[p]:
            p += 1
        break
    s = 0
    ms = m - p
    while s < ms:
        step = min(chunk, ms - s)
        if old[lo - s - step:lo - s] == new[ln - s - step:ln - s]:
            s += step
            continue
        e = s + step
        while s < e and old[lo - 1 - s] == new[ln - 1 - s]:
            s += 1
        break
    return p, new[p:ln - s]


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


