"""Document lines to displayed rows, and the folds that hide lines.

`Rows` is the one mapping the editor uses in both directions; nothing else
adds or subtracts hidden lines. `Folds` keeps which ranges exist and which are
collapsed, by content key, so edits that shift lines never orphan a fold.
"""
from bisect import bisect_right


class Rows:
    """Displayed rows over `count` document lines with `folds` collapsed.

    A fold is `(head, last)`: `head` stays visible, `head + 1 .. last` hide.
    Nested and overlapping folds merge. All lookups are O(log folds).
    """
    __slots__ = ('lines', 'count', 'first', 'last', 'gone', 'shown')

    def __init__(self, lines, folds=()):
        self.lines = max(1, lines)
        first, last = [], []
        for head, end in sorted(folds):
            end = min(end, self.lines - 1)
            if end <= head or head < 0:
                continue
            if first and head <= last[-1]:
                # Its head is hidden by the previous run: one run covers both.
                last[-1] = max(last[-1], end)
            else:
                first.append(head + 1)
                last.append(end)
        # gone[i]: lines hidden through run i. shown[i]: the row after run i.
        self.first, self.last, self.gone, self.shown = first, last, [], []
        hidden = 0
        for start, end in zip(first, last):
            hidden += end - start + 1
            self.gone.append(hidden)
            self.shown.append(end + 1 - hidden)
        self.count = self.lines - hidden

    def line(self, row):
        """The document line displayed on `row` (clamped)."""
        row = min(max(row, 0), self.count - 1)
        run = bisect_right(self.shown, row) - 1
        return row + self.gone[run] if run >= 0 else row

    def row(self, line):
        """The row showing `line`; a hidden line answers its fold head's row."""
        line = min(max(line, 0), self.lines - 1)
        run = bisect_right(self.first, line) - 1
        if run < 0:
            return line
        if line <= self.last[run]:
            line = self.first[run] - 1
            return line - (self.gone[run - 1] if run else 0)
        return line - self.gone[run]

    def hidden(self, line):
        run = bisect_right(self.first, line) - 1
        return run >= 0 and line <= self.last[run]

    def folded(self, line):
        """Lines hidden under `line` when it heads a collapsed run, else 0."""
        run = bisect_right(self.first, line + 1) - 1
        if run >= 0 and self.first[run] == line + 1:
            return self.last[run] - line
        return 0

    def span(self, start, stop):
        """Document lines of rows `start .. stop - 1`."""
        stop = min(stop, self.count)
        return [self.line(row) for row in range(max(start, 0), stop)] if self.first \
            else list(range(max(start, 0), stop))


class Folds:
    """Foldable line ranges and their collapsed state.

    `ranges` are `(head, last)` sorted by head; `keys` names each by content so
    `collapsed` (a set of keys, the persisted part) survives line shifts.
    """
    __slots__ = ('ranges', 'keys', 'collapsed', 'heads', 'seeded')

    def __init__(self):
        self.ranges, self.keys, self.collapsed, self.heads, self.seeded = [], {}, set(), {}, False

    def set(self, ranges, keys, defaults=()):
        """Replace the ranges. `defaults` collapse once, on the first set."""
        self.ranges = sorted(ranges)
        self.keys = keys
        self.heads = {}
        for item in self.ranges:
            # The outermost range wins a shared head line.
            self.heads.setdefault(item[0], item)
        if not self.seeded:
            self.seeded = True
            self.collapsed.update(keys[item] for item in defaults if item in keys)

    def closed(self):
        """The collapsed ranges, for `Rows`."""
        collapsed, keys = self.collapsed, self.keys
        return [item for item in self.ranges if keys[item] in collapsed] if collapsed else []

    def at(self, line):
        """The range headed by `line`, or None."""
        return self.heads.get(line)

    def is_closed(self, item):
        return self.keys.get(item) in self.collapsed

    def toggle(self, line):
        item = self.heads.get(line)
        if item is None:
            return False
        self.collapsed ^= {self.keys[item]}
        return True

    def reveal(self, line):
        """Open every collapsed range hiding `line`. True if any opened."""
        keys = {self.keys[(head, last)] for head, last in self.ranges
                if head < line <= last} & self.collapsed
        self.collapsed -= keys
        return bool(keys)

    def set_all(self, closed, skip=()):
        """Collapse or expand the outermost ranges (and nothing in `skip`)."""
        outer, end = [], -1
        for head, last in self.ranges:
            if head > end:
                outer.append((head, last))
                end = last
        keys = {self.keys[item] for item in outer if item not in skip}
        self.collapsed = self.collapsed | keys if closed else self.collapsed - keys
