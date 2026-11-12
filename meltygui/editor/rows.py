"""Document lines to displayed rows, and the folds that hide lines.

`Rows` is the one mapping the editor uses in both directions, and it holds
the fold layers that make it: the scopes a `Source` reads from the text and
the caller's ranges, each with its own collapsed set. It lives in Rust
(native/source/src/rows.rs: read there how to change it), so following an
edit or taking a fresh reading costs nothing per fold.

    rows = Rows(lines, collapsed, seeded, gaps)   # the last two sessions' collapsed names and caller ranges
    rows.count, rows.row(line), rows.line(row), rows.hidden(line), rows.folded(line), rows.before(line)
    rows.fold(line), rows.toggle(line), rows.reveal(line), rows.fold_all(closed), rows.guides(first, last)
    rows.set(heads, close), rows.levels(close)    # many folds at once: what the fold chords use (editor/keys.py)
    rows.follow(line, column, gone, came)         # every edit
    rows.land(source.native, changes, first, last)  # fresh scope ranges (`Source.ask(rows=rows)`)
    rows.gaps(ranges)                             # the caller's ranges

`Numbers` maps buffer lines to the file lines the gutter shows.
"""
from meltygui.model.source_model import Rows   # noqa: F401


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
