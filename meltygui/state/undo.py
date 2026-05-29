import collections

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


class Change:
    """One recorded edit: a draw_state's value moving from `old` to `new`."""

    def __init__(self, draw_state, old, new):
        self.draw_state = draw_state
        self.old = old
        self.new = new

    def __repr__(self):
        name = getattr(self.draw_state, "name", "?")
        return f"Change({name}: {self.old!r} -> {self.new!r})"


@window(view_func=RenderFuncs.draw_type)
class UndoManager:
    # draw_state -> ordered list of Changes recorded for that node. Safe to key
    # on the draw_state object: DrawState uses identity equality and hashes on
    # its unique id, so distinct nodes never collide as keys.
    change_history = {}

    # Global timeline of every Change in the order it happened. This is the
    # companion that bounds total size: when this grows past MAX_HISTORY the
    # oldest Change is dropped from here and from its per-node list above.
    history = collections.deque()

    MAX_HISTORY = 514

    settle_for = 100 # 2 frame after start

    @classmethod
    def record(cls, draw_state, old, new):
        if Core.melty.frame_count < cls.settle_for:
            return
        change = Change(draw_state, old, new)
        cls.change_history.setdefault(draw_state, []).append(change)
        cls.history.append(change)

        while len(cls.history) > cls.MAX_HISTORY:
            evicted = cls.history.popleft()
            per_node = cls.change_history.get(evicted.draw_state)
            if per_node:
                per_node.remove(evicted)
                if not per_node:
                    del cls.change_history[evicted.draw_state]


def handle_undo(changed, old_value, new_value, draw_state):
    if not changed:
        return
    UndoManager.record(draw_state, old_value, new_value)
