"""The decoration layer: marks on lines that follow edits, and their producers.

Everything the editor shows besides the text is a `Mark` in a `Marks` set:
line marks (an error, a paused line), span marks (a search hit, a symbol
wash) and gutter marks (a breakpoint). A mark names a `shape`; the view knows
how each shape paints and on which layer (under the text, the glyph colour,
over the text, the gutter).

`Lines` is the one structure that keeps line numbers right while the text
changes; `Marks` is a sorted set over it, tagged with the document version it
was computed for. A producer (a lint run, a search, a tint pass) builds a
`Marks` wherever it likes, including on a worker thread, and hands it to
`Editor.put`. From then on the set follows every edit by `Marks.follow`, so
stale results stay on their text until fresh ones replace them. A set belongs
to the one editor it was given to.
"""
from bisect import bisect_left
from collections import namedtuple

# One decoration: `shape` says how it is painted, `low` and `high` are its
# columns (`high` None: the whole line), `data` whatever the shape needs.
Mark = namedtuple('Mark', 'shape low high color data', defaults=(0, None, None, None))
# One live value on a line: its name, short text (None when not simple) and the value.
Value = namedtuple('Value', 'name text value')


class Lines:
    """Sorted line numbers that follow edits. An edit moves every line below
    it, so the entries from `gap` on share one pending `shift`: an edit next
    to the previous one costs nothing, one elsewhere moves the gap there."""
    __slots__ = ('at', 'gap', 'shift')

    def __init__(self, lines=()):
        self.at = list(lines)
        self.gap, self.shift = len(self.at), 0

    def __len__(self):
        return len(self.at)

    def line(self, index):
        return self.at[index] + self.shift if index >= self.gap else self.at[index]

    def find(self, line):
        """Index of the first entry at or below `line`."""
        at, gap = self.at, self.gap
        if gap and at[gap - 1] >= line:
            return bisect_left(at, line, 0, gap)
        return bisect_left(at, line - self.shift, gap)

    def seek(self, index):
        """Move the gap to `index`."""
        at, gap, shift = self.at, self.gap, self.shift
        if shift and index > gap:
            at[gap:index] = [line + shift for line in at[gap:index]]
        elif shift and index < gap:
            at[index:gap] = [line - shift for line in at[index:gap]]
        self.gap = index

    def move(self, index, delta):
        """The entries from `index` on are `delta` lines lower."""
        if delta:
            self.seek(index)
            self.shift += delta

    def put(self, index, line):
        """Set one entry's line; the caller keeps the order."""
        self.at[index] = line - self.shift if index >= self.gap else line

    def drop(self, low, high):
        del self.at[low:high]
        if self.gap >= high:
            self.gap -= high - low
        elif self.gap > low:
            self.gap = low


class Marks:
    """Decorations sorted by (line, low column). `version` is the document
    version they were computed for (None: the current one)."""
    __slots__ = ('lines', 'items', 'version')

    def __init__(self, marks=(), version=None):
        """`marks`: (line, Mark) pairs in order."""
        marks = list(marks)
        self.lines = Lines([line for line, _ in marks])
        self.items = [mark for _, mark in marks]
        self.version = version

    def __len__(self):
        return len(self.items)

    def on(self, first, last=None):
        """[(line, mark)] on lines `first .. last`."""
        lines, items = self.lines, self.items
        low, high = lines.find(first), lines.find((first if last is None else last) + 1)
        return [(lines.line(at), items[at]) for at in range(low, high)]

    def spot(self, line, column):
        """Index of the first mark at or after (line, column)."""
        lines, items = self.lines, self.items
        at, count = lines.find(line), len(items)
        while column and at < count and lines.line(at) == line and items[at].low < column:
            at += 1
        return at

    def follow(self, change):
        """Keep every mark on its text through one edit: marks inside the
        removed text go (a whole line mark only with its whole line), marks
        after it move, and a span the edit starts in ends where the edit
        starts. A whole line mark belongs to its line's text from column 0:
        a line break typed there pushes it down, and a line joined onto the
        one above takes its mark along."""
        lines, items = self.lines, self.items
        first, column, last = change.line, change.column, change.line + change.gone
        low, high = self.spot(first, column), self.spot(last, change.old)
        if column:
            for at in range(lines.find(first), low):
                mark = items[at]
                if mark.high is not None and mark.high > column:
                    grown = mark.high - change.old + change.new if not change.gone and mark.high >= change.old else column
                    items[at] = mark._replace(high=max(grown, column))
        if high > low:
            # A whole line mark stays while any of its line does: the last line's.
            kept = [items[at] for at in range(max(lines.find(last), low), high) if items[at].high is None]
            if kept:
                high -= len(kept)
                items[high:high + len(kept)] = kept
            del items[low:high]
            lines.drop(low, high)
        shift = change.new - change.old
        at, count = low, len(items)
        while shift and at < count and lines.line(at) == last:
            mark = items[at]
            if mark.high is not None:
                items[at] = mark._replace(low=mark.low + shift, high=mark.high + shift)
            at += 1
        lines.move(low, change.came - change.gone)

    def follow_all(self, changes):
        for change in changes:
            self.follow(change)
        return self


def current(marks, doc):
    """`marks` brought forward to the document's version, or None when the
    edits since it was computed are no longer known."""
    if marks is None or marks.version is None or marks.version == doc.version:
        return marks
    changes = doc.since(marks.version)
    if changes is None:
        return None
    marks.version = doc.version
    return marks.follow_all(changes)


# --- producers ----------------------------------------------------------------
# Each turns one model's values into a `Marks` set. A feature that wants to
# show something in the editor writes one of these and nothing else: the view
# paints by shape (`SHAPES` in view/editor_view.py), the session follows edits.
# Lines given by callers are file lines: `numbers` (`rows.Numbers`) maps
# them to buffer lines, which is what marks are kept in.
def given(found):
    """A caller's ready-made set."""
    return found


def problems(found, imports, numbers):
    """Errors, and the import statements that would fix them, from
    `[(line, message)]`, an exception or a parse tree carrying error markers,
    and `{line: [statement]}`. A line gets an underline, a red number and
    the message after its text; the 'error' mark carries (message, fixes)."""
    if isinstance(found, BaseException):
        from meltygui.editor.syntax import tools
        found = tools()._exception_errors(found)
    notes = {}
    for number, message in found or ():
        line = numbers.line(max(int(number), numbers.first if numbers.table is None else 0))
        if line is not None:
            notes[line] = f'{notes[line]}; {message}' if line in notes else str(message)
    fixes = {numbers.line(int(number)): list(statements) for number, statements in (imports or {}).items()}
    marks = []
    for line in sorted(notes):
        marks += [(line, Mark('under')), (line, Mark('number')), (line, Mark('error', data=(notes[line], fixes.get(line, []))))]
    return Marks(marks)


def values(store, numbers):
    """The live values of a store (`code/live_view.py`) as 'value' marks
    after their lines."""
    if store is None:
        return None
    from meltygui.code.live_view import live_values_for
    from meltygui.editor.live_view_views import _split_line_key, _inline_value_text
    marks = []
    for key, value in live_values_for(store).items():
        name, number = _split_line_key(key[-1])
        line = numbers.line(number) if name is not None else None
        if line is not None:
            marks.append((line, Mark('value', data=Value(name, _inline_value_text(value), value))))
    marks.sort(key=lambda item: item[0])
    return Marks(marks)


def breaks(found, numbers):
    """Breakpoint dots in the gutter from a set of file lines."""
    if found is None:
        return None
    lines = (numbers.line(number) for number in found)
    return Marks([(line, Mark('dot')) for line in sorted(line for line in lines if line is not None)])


def pause(number, numbers):
    """The debugger's line: a band across it and an arrow in the gutter."""
    line = numbers.line(number) if number is not None else None
    return Marks([(line, Mark('band')), (line, Mark('arrow'))]) if line is not None else None


def tints(found, text, version=None):
    """Definition tints as a `Marks` set: a 'tint' mark on each tinted
    definition's first line and a 'wash' over each use of its name. `found`
    is the (blocks, spans, ...) result of `roster_tints.collect_def_tints` or
    the legacy `_collect_def_tints` for `text`. Reads the whole text: call it
    where the tints are computed, off the render thread."""
    if not found:
        return Marks((), version)
    starts, at = [0], text.find('\n')
    while at != -1:
        starts.append(at + 1)
        at = text.find('\n', at + 1)
    lines, marks = text.split('\n'), []
    for head, _, last, rgb in found[0]:
        # A tinted block starts at its decorators: the mark goes on the line that heads the scope.
        while head < last and not lines[head].lstrip().startswith(('def ', 'class ', 'async ')):
            head += 1
        marks.append((head, 0, Mark('tint', 0, None, tuple(rgb[:3]))))
    for start, stop, rgb, *_ in found[1]:
        line = bisect_left(starts, start + 1) - 1
        column = start - starts[line]
        marks.append((line, column, Mark('wash', column, column + stop - start, tuple(rgb[:3]))))
    marks.sort(key=lambda item: item[:2])
    return Marks([(line, mark) for line, _, mark in marks], version)


def hits(text, term, version=None):
    """Every occurrence of `term` in `text` (case blind) as 'hit' marks."""
    marks = []
    if term:
        needle = term.lower()
        for line, row in enumerate(text.lower().split('\n')):
            at = row.find(needle)
            while at >= 0:
                marks.append((line, Mark('hit', at, at + len(needle))))
                at = row.find(needle, at + len(needle))
    return Marks(marks, version)
