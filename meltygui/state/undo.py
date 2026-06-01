import collections

import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header


class Change:
    """One recorded edit: a draw_state's value moving from `old` to `new`. `ui`
    is a snapshot of the draw_state's transient UI state (caret/selection/scroll)
    captured before the edit, restored alongside `old` on undo."""

    def __init__(self, draw_state, old, new, ui=None):
        self.draw_state = draw_state
        self.old = old
        self.new = new
        self.ui = ui
    #
    # def __repr__(self):
    #     name = getattr(self.draw_state, "name", "?")
    #     return f"Change({name}: {self.old!r} -> {self.new!r})"


@render_func(show_bg=True, use_cache=True, shadow=False, with_header=draw_header, temp=True)
def draw_undo_manager(input_value, **kwargs):
    """Render the undo history as one readable line per recorded change, newest
    first. input_value is the UndoManager class (handed in by @window), so we
    read its `history` deque directly — works like draw_type, just formatted as
    change lines instead of a generic object dump."""
    history = getattr(input_value, "history", ())
    imgui.text(f"{len(history)} change(s)")
    if history:
        imgui.separator()
    for idx, change in enumerate(reversed(history)):
        name = getattr(change.draw_state, "name", None) or "?"

        from_val = str(change.old)[:20] # truncate long values for readability
        to_val = str(change.new)[:20]
        imgui.text(f"{name}: {from_val} -> {to_val}")

        if idx >= 10:
            imgui.text(f"... and {len(history) - 10} more")
            break
    return False, input_value


@window(view_func=draw_undo_manager, live=True)
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
    APPROVED_TYPES = (float, int, str, bool, tuple)

    # Global timeline of every Change in the order it happened. This is the
    # companion that bounds total size: when this grows past MAX_HISTORY the
    # oldest Change is dropped from here and from its per-node list above.
    history = collections.deque(maxlen=MAX_HISTORY)

    settle_for = 100 # 2 frame after start

    @classmethod
    def undo(cls):
        # Pop the newest recorded change and register a request with Melty for its
        # draw_state. core_render intercepts that draw_state's return next frame and
        # reports (True, old_value), which the parent collection writes back into its
        # model - exactly as if the user had typed the previous value.
        if not cls.history:
            return
        change = cls.history.pop()
        ds = change.draw_state
        Core.melty.undo_requests[ds] = change

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

        change = Change(draw_state, old, new, ui=getattr(draw_state, "_undo_pre", None))
        # cls.change_history.setdefault(draw_state, []).append(change)
        cls.history.append(change)

        # while len(cls.history) > cls.MAX_HISTORY:
        #     evicted = cls.history.popleft()
        #     per_node = cls.change_history.get(evicted.draw_state)
        #     if per_node:
        #         per_node.remove(evicted)
        #         if not per_node:
        #             del cls.change_history[evicted.draw_state]


def handle_undo(changed, old_value, new_value, draw_state):
    if not changed:
        return
    # Only the widget the user directly interacted with may record an undo entry.
    # A single edit bubbles up through every render_func chain - the focused text
    # editor (draw_text) and its converter/wrapper ancestors (code_to_io_wrapped,
    # collections) all report the same string change, often on different frames
    # because conversion runs async. Those pass-through draw_states must never
    # become undo-stack elements; only the original origin counts. That origin
    # is the focused text editor or the imgui-active widget, skip; a
    # container/converter is neither.
    is_origin = (draw_state is Core.melty.text_focused_ds
                 or getattr(draw_state, "_imgui_is_edited", False))
    if not is_origin:
        return
    UndoManager.record(draw_state, old_value, new_value)
