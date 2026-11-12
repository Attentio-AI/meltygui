"""The edited text, seen through its `Source`, with the caret.

The text lives in the `Source` (model/source_model.py: Rust) and nowhere
else, with its version and the log of its splices; several documents may be
on one source. `Document` is one session's face on it: lines, columns and
offsets, this session's caret and selection, and `splice`, its way to edit.
Nothing here holds or walks the text: every answer is asked of the source
for the lines or offsets it concerns, so an edit or a jump costs its own
lines whatever the file size.

A block is a run of about `BLOCK` lines that the view paints as one
(`blocks`): its key stays with it through edits, the blocks below an edit
only move. Offsets index the whole text; lines and columns are 0 based.
`Document.text` is the whole text as one string: a copy, so nothing on the
path of a keystroke should ask for it.
"""
from meltygui.model.source_model import Source, Splice, LINES, LOG   # noqa: F401  (`Splice`, `LOG`: what edits are told as)

# Lines in a fresh block. One block is one `block` view: smaller blocks
# repaint less per keystroke, larger ones declare fewer views. The lever is
# `LINES` in native/source/src/store.rs.
BLOCK = LINES
# Lever: characters before the caret that context helpers may read (bracket
# matching for indents looks back this far in the legacy editor too).
NEAR = 4000


class Document:
    __slots__ = ('source', 'store', 'caret', 'anchor', 'goal', 'joined', 'whole', 'record', 'by')

    def __init__(self, text='', source=None):
        """`source`: the `Source` holding the text, which this document
        edits along with whoever else does; without one, one is made for `text`."""
        self.source = source if source is not None else Source(text, version=1)
        self.store = self.source.native
        # Undo hook: `record(low, old, new, before, after)` for every `replace` (the session's, see history.py).
        self.record = None
        # Who the source is told made this document's edits: its session.
        self.by = self
        self.caret = self.anchor = 0
        self.goal = None
        self.whole, self.joined = None, -1

    # The version of the text: the source's, which rises with every splice, whoever makes it.
    version = property(lambda self: self.store.version)
    lines = property(lambda self: self.store.lines)
    size = property(lambda self: self.store.size)

    @property
    def text(self):
        """The whole text as one string."""
        if self.joined != self.version:
            self.whole, self.joined = self.store.text(), self.version
        return self.whole

    # --- lines -----------------------------------------------------------
    def blocks(self, first, last):
        """[(key, first line, lines, stamp)] of the blocks holding lines
        `first .. last`; `stamp` is the version of the edit that last rewrote the block."""
        return self.store.blocks(first, last)

    def rows(self, first, count):
        """The text of `count` lines from `first`, without newlines."""
        return self.store.texts(first, count)

    def line(self, line):
        return self.store.texts(line, 1)[0]

    def line_of(self, index):
        return self.store.place(index)[0]

    def start(self, line):
        return self.store.start(line)

    def end(self, line):
        """Offset of the line's newline (or the text end)."""
        return self.store.start(line + 1) - 1 if line + 1 < self.lines else self.size

    def place(self, index):
        """(line, column) of an offset."""
        return self.store.place(index)

    def index(self, line, column):
        line = min(max(line, 0), self.lines - 1)
        return min(self.start(line) + max(column, 0), self.end(line))

    def slice(self, low, high):
        """The text between two offsets."""
        return self.store.slice(low, high)

    def near(self, index):
        """(text, base): the text around offset `index`, from at least
        `NEAR` characters before it to the end of the next block, and the
        offset it starts at. What a helper that scans around the caret gets."""
        return self.store.near(index, NEAR)

    # --- selection -------------------------------------------------------
    @property
    def selection(self):
        """(low, high) offsets; equal when nothing is selected."""
        return (self.caret, self.anchor) if self.caret < self.anchor else (self.anchor, self.caret)

    def select(self, caret, anchor=None):
        size = self.size
        self.caret = min(max(caret, 0), size)
        self.anchor = self.caret if anchor is None else min(max(anchor, 0), size)
        self.goal = None

    # --- editing ---------------------------------------------------------
    def replace(self, low, high, new, caret=None):
        """An edit command's change: put `new` in place of the text from
        `low` to `high`, move the caret after it (or to `caret`) and report
        it to the undo hook. Returns the `Splice`, or None when nothing changed."""
        old = self.slice(low, high)
        before = (self.caret, self.anchor)
        after = low + len(new) if caret is None else caret
        if old == new:
            self.select(after)
            return None
        change = self.splice(low, high, new)
        self.select(after)
        if self.record is not None:
            self.record(low, old, new, before, (self.caret, self.anchor))
        return change

    def splice(self, low, high, new):
        """The raw edit, without an undo step: the source rewrites the block
        the edit is in, which keeps its key; the blocks below only move."""
        return self.source.splice(low, high, new, by=self.by)

    def since(self, version):
        """The splices made after `version` by anyone, or None when they are no longer kept."""
        found = self.source.since(version)
        return None if found is None else [entry[1] for entry in found]
