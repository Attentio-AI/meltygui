"""The edited text: one string, its line starts, the caret and undo history.

Every change goes through `Document.replace`, which keeps the line table,
version and history in step and reports which lines changed. Offsets index
`text`; lines and columns are 0 based.
"""
from bisect import bisect_right
from collections import namedtuple

# Lines `line .. line + removed - 1` became `line .. line + added - 1`;
# `old` is the text those lines had.
Change = namedtuple('Change', 'line removed added old')
# Lever: undo steps kept.
HISTORY = 2000


class Document:
    __slots__ = ('text', 'starts', 'caret', 'anchor', 'goal', 'version', 'undone', 'redone', 'changes')

    def __init__(self, text=''):
        self.undone, self.redone, self.version = [], [], 0
        self.changes = []
        self.load(text)

    def load(self, text):
        """Replace the whole text from outside (not an undo step)."""
        self.text = text
        starts, find, at = [0], text.find, text.find('\n')
        while at != -1:
            starts.append(at + 1)
            at = find('\n', at + 1)
        self.starts = starts
        self.caret = self.anchor = min(getattr(self, 'caret', 0), len(text))
        self.goal = None
        self.version += 1
        self.undone.clear()
        self.redone.clear()

    # --- lines -----------------------------------------------------------
    @property
    def lines(self):
        return len(self.starts)

    def line_of(self, index):
        return bisect_right(self.starts, index) - 1

    def start(self, line):
        return self.starts[line]

    def end(self, line):
        """Offset of the line's newline (or the text end)."""
        starts = self.starts
        return starts[line + 1] - 1 if line + 1 < len(starts) else len(self.text)

    def line(self, line):
        return self.text[self.starts[line]:self.end(line)]

    def place(self, index):
        """(line, column) of an offset."""
        line = bisect_right(self.starts, index) - 1
        return line, index - self.starts[line]

    def index(self, line, column):
        line = min(max(line, 0), len(self.starts) - 1)
        return min(self.starts[line] + max(column, 0), self.end(line))

    # --- selection -------------------------------------------------------
    @property
    def selection(self):
        """(low, high) offsets; equal when nothing is selected."""
        return (self.caret, self.anchor) if self.caret < self.anchor else (self.anchor, self.caret)

    def select(self, caret, anchor=None):
        size = len(self.text)
        self.caret = min(max(caret, 0), size)
        self.anchor = self.caret if anchor is None else min(max(anchor, 0), size)
        self.goal = None

    # --- editing ---------------------------------------------------------
    def replace(self, low, high, new, caret=None, merge=False):
        """Put `new` in place of `text[low:high]` and move the caret after it
        (or to `caret`). `merge` joins the step to an adjoining typed one.
        Returns the `Change`, or None when nothing changed."""
        text = self.text
        old = text[low:high]
        before = (self.caret, self.anchor)
        after = low + len(new) if caret is None else caret
        if old == new:
            self.select(after)
            return None
        change = self.splice(low, high, new)
        self.select(after)
        undone = self.undone
        last = undone[-1] if undone else None
        if merge and last is not None and last[5] and last[0] + len(last[2]) == low and not old:
            undone[-1] = (last[0], last[1], last[2] + new, last[3], after, True)
        else:
            undone.append((low, old, new, before, after, merge))
            if len(undone) > HISTORY:
                del undone[0]
        self.redone.clear()
        return change

    def splice(self, low, high, new):
        """The raw text and line-table edit, without history."""
        text, starts = self.text, self.starts
        first = bisect_right(starts, low) - 1
        last = bisect_right(starts, high) - 1
        old = text[starts[first]:starts[last + 1] - 1 if last + 1 < len(starts) else len(text)]
        self.text = text[:low] + new + text[high:]
        fresh, find, at = [], new.find, new.find('\n')
        while at != -1:
            fresh.append(low + at + 1)
            at = find('\n', at + 1)
        delta = len(new) - (high - low)
        tail = [start + delta for start in starts[last + 1:]] if delta else starts[last + 1:]
        starts[first + 1:] = fresh + tail
        self.version += 1
        change = Change(first, last - first + 1, len(fresh) + 1, old)
        self.changes.append(change)
        return change

    def undo(self):
        if not self.undone:
            return None
        step = self.undone.pop()
        low, old, new, before = step[:4]
        change = self.splice(low, low + len(new), old)
        self.select(*before)
        self.redone.append(step)
        return change

    def redo(self):
        if not self.redone:
            return None
        step = self.redone.pop()
        low, old, new, _, after = step[:5]
        change = self.splice(low, low + len(old), new)
        self.select(after)
        # A redone step never merges with later typing.
        self.undone.append(step[:5] + (False,))
        return change
