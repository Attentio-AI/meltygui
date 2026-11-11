"""The editor's undo steps, on the framework's undo stack.

The editor keeps no history of its own. Every edit becomes a `Step`, a
`Change` like any other view's, pushed on `UndoManager.stack`; Ctrl+Z is the
framework's hotkey, which pops the stack and has each change re-apply itself.
A step holds the splices of one command, never whole texts, so a keystroke in
a multi megabyte buffer records a few characters.

Typing folds into the step before it by the framework's own rule
(`UndoManager._typing_continues`, here for splices): same direction, at the
run's caret edge, until a new word starts. Importing this module loads the
undo manager; `syntax.warm` does that off the render thread.
"""
import time

from meltygui.core.rendering.core_decoration import Core
from meltygui.state.core_undo import Change, UndoManager, _TypingEdit, _run_edge, _word_starts


class Step(Change):
    """`parts` are (offset, old text, new text) in the order they were made;
    `before` and `after` the (caret, anchor) around them. `version` is the
    document version the step left: typing only folds into a step the
    document has not moved on from."""

    def __init__(self, ed, part, before, after, kind, group):
        low, old, new = part
        edge = _run_edge(_TypingEdit(kind, low, new or old), low, None) if kind else (None, '')
        super().__init__(ed, old, new, t=time.time(), direction=kind or 'replace', group_id=group,
                         frame=Core.melty.frame_count, edit_end=edge[0], edge_char=edge[1], sealed=kind is None)
        self.parts, self.before, self.after = [part], before, after
        self.version = ed.doc.version
        self.drag = None           # the offset of the token an inline widget is dragging

    @property
    def display_name(self):
        return 'text'

    def apply(self, undo):
        self.draw_state.replay(self, undo)


def typing(old, new):
    """'insert' or 'delete' for a keystroke-sized edit, None for a standalone one."""
    from meltygui.core.runtime.toggles import Toggles
    text = new or old
    if (old and new) or not text or '\n' in text or len(text) > Toggles.CodeEditor.undo_typing_max_chars:
        return None
    return 'insert' if new else 'delete'


def record(ed, low, old, new, before, after):
    """Put one splice of `ed`'s document on the undo stack: into the step of
    the running command, into the typing run before it, or as a new step."""
    stack, doc = UndoManager.stack, ed.doc
    top = stack.history[-1] if stack.history else None
    mine = isinstance(top, Step) and top.draw_state is ed and top.version == doc.version - 1
    stack.redo_stack.clear()
    if mine and top is ed.step:
        # A later splice of the same command: one step, never a typing run.
        if ed.dragging and top.drag == low == top.parts[-1][0]:
            # An inline widget dragging its token on: the step keeps the text the drag started from.
            top.parts[-1] = (low, top.parts[-1][1], new)
        else:
            top.parts.append((low, old, new))
        top.new, top.after, top.version, top.sealed = new, after, doc.version, True
        return
    kind = typing(old, new)
    if mine and kind and not top.sealed and top.direction == kind and len(top.parts) == 1:
        start, was, now = top.parts[0]
        text = new or old
        if kind == 'insert' and low == top.edit_end:
            part, typed = (start, was, now + text), text
        elif kind == 'delete' and low + len(text) == top.edit_end:      # Backspace
            part, typed = (low, text + was, now), text[::-1]
        elif kind == 'delete' and low == top.edit_end:                  # Delete key
            part, typed = (start, was + text, now), text
        else:
            part = None
        from meltygui.core.runtime.toggles import Toggles
        if part is not None and not (Toggles.CodeEditor.undo_word_steps and _word_starts(top.edge_char + typed)):
            top.parts[0] = part
            top.edit_end, top.edge_char = _run_edge(_TypingEdit(kind, low, text), low, top.edit_end)
            top.old, top.new, top.after, top.version = part[1], part[2], after, doc.version
            top.t, top.frame = time.time(), Core.melty.frame_count
            ed.step = top
            return
    # A new step. It joins the group of a change another view recorded for
    # the same user action (a frame or two ago), as `UndoManager.record` does.
    group = None
    if top is not None and Core.melty.frame_count - top.frame <= UndoManager.GROUP_FRAME_WINDOW:
        group = top.group_id
        for change in reversed(stack.history):
            if change.group_id != group:
                break
            if change.draw_state is ed:
                group = None
                break
    ed.step = Step(ed, (low, old, new), before, after, kind, stack.new_group_id() if group is None else group)
    if ed.dragging:
        ed.step.drag, ed.step.sealed = low, True
    stack.history.append(ed.step)


def rebase(ed, low, high, size):
    """The text of `ed` changed without a step (its caller replaced
    `text[low:high]` by `size` characters): move the offsets of the steps
    that are still exact, newest first, and drop the ones the change reaches
    into, with every older one. Redone steps of this editor go as well."""
    stack = UndoManager.stack
    for group in list(stack.redo_stack):
        group[:] = [change for change in group if change.draw_state is not ed]
        if not group:
            stack.redo_stack.remove(group)
    delta, lost = size - (high - low), False
    for change in [change for change in reversed(stack.history) if change.draw_state is ed]:
        for at in range(len(change.parts) - 1, -1, -1):
            start, old, new = change.parts[at]
            if lost:
                break
            if start + len(new) <= low:
                # The part is before the change: seen from before the part, the change sits elsewhere.
                low, high = low + len(old) - len(new), high + len(old) - len(new)
            elif start >= high:
                change.parts[at] = (start + delta, old, new)
            else:
                lost = True
        if lost:
            stack.history.remove(change)
