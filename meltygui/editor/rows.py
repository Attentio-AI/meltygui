"""Document lines to displayed rows, and the folds that hide lines.

`Rows` is the one mapping the editor uses in both directions; nothing else
adds or subtracts hidden lines. `Folds` is one layer of foldable ranges: which
exist, which are collapsed (by content key, so edits that shift lines never
orphan a fold) and how they nest. A layer follows edits by `Folds.follow`
until a fresh scan replaces it; the editor stacks layers (scopes, caller
ranges) and builds one `Rows` from all their collapsed ranges.
"""
from bisect import bisect_right

from meltygui.editor.marks import Lines


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

    def before(self, line):
        """Rows above `line`: the shown lines before it."""
        run = bisect_right(self.first, line) - 1
        if run < 0:
            return line
        if line > self.last[run]:
            return line - self.gone[run]
        return self.first[run] - (self.gone[run - 1] if run else 0)

    def span(self, start, stop):
        """Document lines of rows `start .. stop - 1`."""
        stop = min(stop, self.count)
        return [self.line(row) for row in range(max(start, 0), stop)] if self.first \
            else list(range(max(start, 0), stop))


class Numbers:
    """Buffer lines to the file lines the gutter shows, and back. Either
    the buffer is a run of its file starting at file line `first`, or
    `table[line]` is each buffer line's file line (None: a row without one,
    as the removed lines of a diff). Every line a caller names (an error, a
    breakpoint, a jump) is a file line and goes through here."""
    __slots__ = ('first', 'table', 'back')

    def __init__(self, first=1, table=None):
        self.first, self.table, self.back = first, table, None

    def number(self, line):
        """The file line of buffer line `line`, or None."""
        if self.table is None:
            return self.first + line
        return self.table[line] if line < len(self.table) else None

    def line(self, number):
        """The buffer line showing file line `number`, or None."""
        if self.table is None:
            return number - self.first if number >= self.first else None
        if self.back is None:
            self.back = {found: line for line, found in enumerate(self.table) if found is not None}
        return self.back.get(number)

    def digits(self, lines):
        """Characters the gutter needs. A table gets a fixed strip, so
        previews of different files line up side by side."""
        return 5 if self.table is not None else max(len(str(self.first + lines - 1)), 3)


def nest(ranges):
    """(heads, spans, parents) of `(head, last)` ranges: sorted outermost
    first, each with the index of the range around it (-1: none)."""
    ranges = sorted(ranges, key=lambda item: (item[0], -item[1]))
    parents, open_ranges = [], []
    for at, (head, last) in enumerate(ranges):
        while open_ranges and ranges[open_ranges[-1]][1] < head:
            open_ranges.pop()
        parents.append(open_ranges[-1] if open_ranges else -1)
        open_ranges.append(at)
    return [head for head, _ in ranges], [last - head for head, last in ranges], parents, ranges


class Folds:
    """One layer of foldable line ranges and their collapsed state.

    Range `at` is lines `heads.line(at) .. + spans[at]`; `names[at]` is its
    content key (None once its first line was deleted) and `scopes[at]` its
    indent when it is a scope or block the view draws a guide for.
    `collapsed` is the persisted part: a set of names.
    """
    __slots__ = ('heads', 'spans', 'parents', 'names', 'scopes', 'where', 'collapsed', 'closed', 'seeded')

    def __init__(self):
        self.heads, self.spans, self.parents, self.names, self.scopes = Lines(), [], [], [], []
        self.where, self.collapsed, self.closed, self.seeded = {}, set(), [], False

    def __len__(self):
        return len(self.spans)

    def set(self, heads, spans, parents, names, scopes=None, defaults=()):
        """Replace the ranges (the lists are kept, not copied). `defaults`
        are names that collapse once, on the first set."""
        self.heads, self.spans, self.parents, self.names = Lines(heads), spans, parents, names
        self.scopes = scopes if scopes is not None else [None] * len(spans)
        self.where = where = {}
        for at, name in enumerate(names):
            # The outermost range wins a shared name.
            where.setdefault(name, at)
        if not self.seeded:
            self.seeded = True
            self.collapsed.update(name for name in defaults if name in where)
        self.closed = sorted(where[name] for name in self.collapsed if name in where)

    def same(self, heads, spans, names):
        """Whether a fresh scan found exactly the ranges already here."""
        mine = self.heads
        mine.seek(len(mine))
        return mine.at == heads and self.spans == spans and self.names == names

    def range(self, at):
        head = self.heads.line(at)
        return head, head + self.spans[at]

    def ranges(self):
        """The collapsed ranges, for `Rows`."""
        names = self.names
        return [self.range(at) for at in self.closed if names[at] is not None]

    def at(self, line):
        """Index of the outermost range headed by `line`, or None."""
        heads, names = self.heads, self.names
        at = heads.find(line)
        while at < len(names) and heads.line(at) == line:
            if names[at] is not None:
                return at
            at += 1
        return None

    def around(self, line):
        """Indexes of the ranges holding `line`, innermost first."""
        heads, spans, parents, names = self.heads, self.spans, self.parents, self.names
        found, at = [], heads.find(line + 1) - 1
        while at >= 0:
            if names[at] is not None and heads.line(at) + spans[at] >= line:
                found.append(at)
            at = parents[at]
        return found

    def within(self, first, last):
        """Indexes of the ranges touching lines `first .. last`, outermost first."""
        heads, names = self.heads, self.names
        return self.around(first)[::-1] + [at for at in range(heads.find(first + 1), heads.find(last + 1))
                                           if names[at] is not None]

    def is_closed(self, at):
        return self.names[at] in self.collapsed

    def flip(self, at, closed=None):
        """Collapse or expand range `at` (default: toggle)."""
        name = self.names[at]
        if closed is None:
            closed = name not in self.collapsed
        if closed:
            self.collapsed.add(name)
        else:
            self.collapsed.discard(name)
        self.closed = sorted(self.where[name] for name in self.collapsed if name in self.where)

    def close(self, names):
        """Collapse the ranges called `names`."""
        self.collapsed.update(names)
        self.closed = sorted(self.where[name] for name in self.collapsed if name in self.where)

    def reveal(self, line):
        """Open every collapsed range hiding `line`. True if any opened."""
        hiding = [at for at in self.closed if self.names[at] is not None
                  and self.heads.line(at) < line <= self.heads.line(at) + self.spans[at]]
        for at in hiding:
            self.flip(at, False)
        return bool(hiding)

    def set_all(self, closed):
        """Collapse or expand the outermost ranges."""
        names = {name for name, parent in zip(self.names, self.parents) if parent < 0 and name is not None}
        self.collapsed = self.collapsed | names if closed else self.collapsed - names
        self.closed = sorted(self.where[name] for name in self.collapsed if name in self.where)

    def follow(self, change):
        """Keep the ranges on their lines through one edit: ranges below
        move, ranges around it stretch, a range whose first line went dies."""
        delta = change.came - change.gone
        if not delta and not change.gone:
            return
        heads, spans, parents, names = self.heads, self.spans, self.parents, self.names
        first, last = change.line, change.line + change.gone
        low = heads.find(first + (1 if change.column else 0))
        high = heads.find(last)
        for at in range(low, high):
            names[at] = None
            heads.put(at, last)
        at = low - 1
        while at >= 0:
            head = heads.line(at)
            if head + spans[at] >= last:
                spans[at] += delta
            elif head + spans[at] >= first:
                spans[at] = max(first + change.came - head, 0)
            at = parents[at]
        heads.move(low, delta)
