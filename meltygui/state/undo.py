import collections
import time
import weakref
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
        # Replay in the order the changes were recorded (undo walked them
        # newest-first). Two changes in one group that affect the same target
        # (a tab switch and the caret placed by it) must end where the LATER one
        # left it, so the last applied is the newest.
        for change in reversed(group):
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

    def file_location(self, undo):
        """The (path, line, instance) this side lands in (dock tooltips /
        tints), or None."""
        return self.old if undo else self.new


class CaretLocation:
    """Where the text caret sits: a focused draw_text and its caret /
    selection offsets. `tile_id` is the identity used for equality and for
    resolving the LIVE draw_state on replay (a rebuilt tile hands out a fresh
    draw_state object for the same tile — the weakref is only the fallback).
    `path`/`instance` are set when the view is a code-editor pane (the tab
    to select and the editor window to raise on replay); `line` is the
    FULL-buffer caret line for coalescing and the dock's target tooltip."""

    __slots__ = ("draw_state_ref", "tile_id", "cursor", "selection_start",
                 "selection_end", "path", "instance", "line")

    def __init__(self, draw_state, cursor, selection_start, selection_end,
                 path=None, instance=0, line=None):
        self.draw_state_ref = weakref.ref(draw_state)
        self.tile_id = draw_state._tile_id
        self.cursor = cursor
        self.selection_start = selection_start
        self.selection_end = selection_end
        self.path = path
        self.instance = instance
        self.line = line

    @property
    def key(self):
        return (self.tile_id, self.cursor, self.selection_start,
                self.selection_end)

    def __eq__(self, other):
        return isinstance(other, CaretLocation) and self.key == other.key

    def __hash__(self):
        return hash(self.key)

    def draw_state(self):
        """The live draw_state for this tile — the cache's current object
        first (rebuilt tiles), the recorded one as fallback, None when the
        view is gone."""
        cache = getattr(Core.melty, "cache", None)
        live = None
        if cache is not None and self.tile_id is not None:
            live = cache.key_to_draw_state.get(self.tile_id)
        return live if live is not None else self.draw_state_ref()

    def __repr__(self):
        if self.path:
            where = self.path.rsplit("/", 1)[-1]
            return f"{where}:{self.line + 1}" if self.line is not None else where
        draw_state = self.draw_state_ref()
        name = str(getattr(draw_state, "name", "?") or "?").split("##")[0]
        return f"{name}@{self.cursor}"


class CaretChange(Change):
    """A caret / text-focus step: `old`/`new` are CaretLocations — the
    focused draw_text and where its caret sat. Consecutive small moves in the
    same view fold into one change (NavUndo.record_caret), so an arrow-key
    walk is one step back. No draw_state on the change itself: replay resolves
    the LIVE view through the location (NavUndo._apply_caret) and writes the
    caret + focus directly, summoning the view's tab / window first."""

    def __init__(self, old_loc, new_loc, t=0.0, group_id=0, frame=0):
        super().__init__(None, old_loc, new_loc, t=t, group_id=group_id,
                         frame=frame)

    @property
    def display_name(self):
        return "caret"

    def apply(self, undo):
        NavUndo._apply_caret(self.old if undo else self.new)

    def file_location(self, undo):
        location = self.old if undo else self.new
        if location is None or not location.path:
            return None
        return (location.path,
                None if location.line is None else location.line + 1,
                location.instance)


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
    Records location moves (editor tab switches, jump-tos), window
    open/close toggles, and caret / text-focus steps (poll_caret, once per
    frame from Melty.end_frame). Driven by Ctrl+Shift+Left/Right (root
    handler in new_core_view) and the Fast Dock back/forward buttons. Gated
    on Toggles.CodeEditor.undo_navigation (caret steps additionally on
    undo_navigation_caret)."""

    stack = UndoStack("navigation")

    # Reentrancy guard: while undo/redo replays, the navigation it triggers
    # (open_in_editor → tab select + jump-to, window closed writes) should not
    # record fresh entries.
    _restoring = False

    # Caret detector state: where the focused draw_text's caret was at the
    # last poll (a CaretLocation), and the frame up to which caret moves are
    # NOT recorded - armed by every other record_* (a jump / tab switch moves
    # the caret itself; the nav step already covers it), by the jump
    # consumer in draw_code_editor (quiet_caret), and by undo/redo (a replay
    # moves the caret too).
    _last_caret = None
    _caret_quiet_until = -1

    @classmethod
    def _recordable(cls):
        from src.lsd.gl_gui.toggles import Toggles
        return (Toggles.CodeEditor.undo_navigation and not cls._restoring
                and Core.melty.frame_count >= UndoManager.settle_for)

    @classmethod
    def quiet_caret(cls, frames=None):
        """Don't record caret moves for the next `frames` frames (default
        UndoManager.GROUP_FRAME_WINDOW): the move about to happen belongs to
        a navigation step that is recorded (or replayed) on its own."""
        if frames is None:
            frames = UndoManager.GROUP_FRAME_WINDOW
        cls._caret_quiet_until = max(cls._caret_quiet_until,
                                     Core.melty.frame_count + frames)

    @classmethod
    def record_location(cls, old_loc, new_loc):
        """Push a location step. Locations are (path, line, instance) tuples
        (line may be None; instance is which code-editor window — replay must
        land in the SAME window, not the primary). Each step is its own
        group."""
        cls.quiet_caret()
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
        cls.quiet_caret()
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
        cls.quiet_caret()
        if not cls._recordable() or closed_before == closed_after:
            return
        cls.stack.push(WindowChange(window_ds, closed_before, closed_after,
                                    t=time.time(),
                                    group_id=cls.stack.new_group_id(),
                                    frame=Core.melty.frame_count))

    # ── Caret / text-focus detection ─────────────────────────────────────────

    @classmethod
    def poll_caret(cls):
        """Once per frame (Melty.end_frame, after apply_move_to_front settled
        text focus): compare the focused draw_text's caret with the last
        poll and record a CaretChange when it moved — a caret move inside
        one view, or text focus hopping to another view (the old side is
        the view that HELD focus last, even if focus was cleared meanwhile).
        Cheap on the steady state: one 4-tuple compare. Never records
        during the quiet frames a jump / tab switch / replay armed, nor the
        caret move of a text EDIT (the edit stack owns that — typing would
        otherwise leave a nav step at every pause)."""
        draw_state = Core.melty.text_focused_ds
        if draw_state is None or getattr(draw_state, "is_search_box", False):
            return
        last = cls._last_caret
        key = (draw_state._tile_id, draw_state.text_cursor_pos,
               draw_state.text_selection_start, draw_state.text_selection_end)
        if last is not None and last.key == key:
            return
        current = cls._caret_location(draw_state)
        cls._last_caret = current
        if last is None or not cls._recordable():
            return
        from src.lsd.gl_gui.toggles import Toggles
        if not Toggles.CodeEditor.undo_navigation_caret:
            return
        frame = Core.melty.frame_count
        if frame <= cls._caret_quiet_until:
            return
        if cls._edited_recently(draw_state, frame):
            return
        cls.record_caret(last, current)

    @classmethod
    def _caret_location(cls, draw_state):
        """Snapshot `draw_state`'s caret as a CaretLocation. A code-editor pane (the
        selected tab of an editor instance, per open_files._active_editors)
        carries its path/instance so replay can re-select the tab, and its
        FULL-buffer caret line (the caret offset lives in fold display
        space). Other draw_texts count lines in their raw input when it's a
        string; line None = coalesce on time alone."""
        pos = draw_state.text_cursor_pos
        path, instance, text = None, 0, None
        from src.lsd.gl_gui.view.playground.open_files import _active_editors
        for editor_instance, (editor_path, pane, held_text) in _active_editors.items():
            if pane is draw_state:
                path, instance, text = editor_path, editor_instance, held_text
                break
        line = None
        if path is not None and isinstance(text, str):
            from src.lsd.gl_gui.view.core_views.text_editor import fold_buffer_line_at
            line = fold_buffer_line_at(draw_state, text, max(0, min(pos, len(text))))
        else:
            raw = getattr(draw_state, "_raw_input_value", None)
            if isinstance(raw, str):
                line = raw.count("\n", 0, max(0, min(pos, len(raw))))
        return CaretLocation(draw_state, pos, draw_state.text_selection_start,
                             draw_state.text_selection_end, path=path,
                             instance=instance, line=line)

    @classmethod
    def _edited_recently(cls, draw_state, frame):
        """Whether the edit stack recorded a change to `draw_state` within the last
        GROUP_FRAME_WINDOW frames — i.e. this caret move came from typing /
        deleting, not from navigating."""
        seen = 0
        for change in reversed(UndoManager.history):
            if frame - change.frame > UndoManager.GROUP_FRAME_WINDOW:
                break
            if change.draw_state is draw_state:
                return True
            seen += 1
            if seen >= 8:
                break
        return False

    @classmethod
    def record_caret(cls, old_loc, new_loc):
        """Push a caret step, or fold it into the newest one: consecutive
        moves in the SAME view within Toggles.CodeEditor.nav_caret_coalesce_s
        seconds of each other that stay within nav_caret_step_lines lines of
        where the step already ended advance that step's `new` (an arrow-key
        walk is one step back), while `old` stays pinned to where the walk
        began. A far move (a click elsewhere, Ctrl+End) or a pause starts a
        new step; so does any move after an undo (a fold would keep a
        diverged redo alive)."""
        if old_loc == new_loc:
            return
        from src.lsd.gl_gui.toggles import Toggles
        now = time.time()
        frame = Core.melty.frame_count
        top = cls.stack.history[-1] if cls.stack.history else None
        if (isinstance(top, CaretChange) and not cls.stack.redo_stack
                and top.new.tile_id == new_loc.tile_id == old_loc.tile_id
                and now - top.t <= Toggles.CodeEditor.nav_caret_coalesce_s
                and (top.new.line is None or new_loc.line is None
                     or abs(top.new.line - new_loc.line)
                     <= Toggles.CodeEditor.nav_caret_step_lines)):
            top.new = new_loc
            top.t = now
            top.frame = frame
            return
        cls.stack.push(CaretChange(old_loc, new_loc, t=now,
                                   group_id=cls.stack.new_group_id(),
                                   frame=frame))

    @classmethod
    def _apply_caret(cls, location):
        """Replay one side of a CaretChange: bring the view on screen (its
        editor tab / window), put the caret back and hand it text focus.
        The view's own caret-follow scroll brings the caret into view on its
        next body run (text_cursor_pos != text_prev_cursor_pos)."""
        draw_state = location.draw_state()
        if draw_state is None:
            return
        from src.lsd.gl_gui.view.playground.open_files import (
            _active_editors, editor_window_draw_state)
        if location.path:
            active = _active_editors.get(location.instance)
            if active is None or active[0] != location.path:
                # Another tab was selected in that editor instance: a line-less
                # location replay selects it (and raises its window) without
                # touching the caret - the write below is what lands it.
                cls._apply_location((location.path, None, location.instance))
            else:
                win = editor_window_draw_state(location.instance)
                if win is not None:
                    win.closed = False
                    Core.melty.move_window_to_front(win)
        else:
            # Reopen any closed window on the view's parent chain, then raise
            # the innermost one (move_window_to_front raises its whole chain).
            innermost = None
            node, steps = draw_state.parent_window, 0
            while node is not None and steps < 16:
                if innermost is None:
                    innermost = node
                if node.closed:
                    node.closed = False
                    if Core.melty.cache is not None and node._tile_id is not None:
                        Core.melty.cache.invalidate_up(node._tile_id, force=True,
                                                       max_depth=4)
                next_window = node.parent_window
                if next_window is node:
                    break
                node, steps = next_window, steps + 1
            if innermost is not None:
                Core.melty.move_window_to_front(innermost)
        draw_state.text_cursor_pos = location.cursor
        draw_state.text_selection_start = location.selection_start
        draw_state.text_selection_end = location.selection_end
        draw_state.text_cursor_blink_time = time.time()
        Core.melty.text_focused_ds = draw_state
        Core.melty._text_focus_grant_frame = Core.melty.frame_count
        # The replayed position is the new location - next poll must not read
        # it back as a fresh move (quiet only all the frames in between).
        cls._last_caret = location
        cache = getattr(Core.melty, "cache", None)
        if cache is not None and draw_state._tile_id is not None:
            cache.invalidate_up(draw_state._tile_id, force=True)
        request_render()

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
        cls.quiet_caret()
        try:
            cls.stack.undo()
        finally:
            cls._restoring = False

    @classmethod
    def redo(cls):
        cls._restoring = True
        cls.quiet_caret()
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


