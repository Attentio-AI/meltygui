"""The edited text as blocks of lines, with the caret.

A `Block` holds about `BLOCK` lines: their text, their local line starts and
whatever is derived from exactly those lines (lexer state, repaint stamp).
Every change goes through `Document.splice`, which rewrites the blocks the
edit touches and nothing else: blocks below keep their content and only their
base line and offset move, which are prefix sums rebuilt on demand. So no
edit costs more than its own blocks, whatever the file size.

Offsets index the whole text; lines and columns are 0 based. `Document.text`
joins the blocks, once per version, for callers that need one string.
"""
from bisect import bisect_right
from collections import namedtuple

# Lever: lines in a fresh block. One block is one `block` view: smaller
# blocks repaint less per keystroke, larger ones declare fewer views.
BLOCK = 32
# What one edit did, in lines and columns: the text from (line, column) over
# `gone` line breaks to column `old` became text over `came` line breaks
# ending at column `new`. Everything that follows edits reads only this.
Splice = namedtuple('Splice', 'line column gone old came new')
# Lever: splices kept for results that were computed for an older version.
LOG = 512
# Lever: characters before the caret that context helpers may read (bracket
# matching for indents looks back this far in the legacy editor too).
NEAR = 4000


class Block:
    """`count` lines of text. Every line ends with its newline except the
    last line of the document."""
    __slots__ = ('key', 'text', 'count', 'cut', 'stamp', 'open', 'exit', 'wide')

    def __init__(self, key, text, count):
        self.key, self.stamp = key, 0
        # Lexer state (syntax.py): `open` at each line, None until first
        # lexed and () after a rewrite; `exit` after the last line.
        self.open = self.exit = None
        self.set(text, count)

    def set(self, text, count):
        self.text, self.count = text, count
        self.cut = None                    # local line starts, found on first use
        self.wide = 0                      # longest line in cells, known once painted
        if self.open is not None:
            self.open = ()                 # lexed again; `exit` stays, to tell whether the lines below changed

    @property
    def starts(self):
        """Local offset of each line's first character."""
        found = self.cut
        if found is None:
            text, found = self.text, [0]
            find, at = text.find, text.find('\n')
            while at != -1:
                found.append(at + 1)
                at = find('\n', at + 1)
            del found[self.count:]
            self.cut = found
        return found

    def lines(self):
        """The text of each line, without newlines."""
        return self.text.split('\n')[:self.count]


def chunks(text, final):
    """[(text, lines)] blocks of `BLOCK` lines from `text`; `final` when it
    holds the document's last line (which has no newline)."""
    lines = text.split('\n')
    if not final:
        lines.pop()
    if len(lines) <= BLOCK * 2:
        return [(text, len(lines))]
    found, last = [], len(lines) - BLOCK
    for at in range(0, len(lines), BLOCK):
        part = lines[at:at + BLOCK]
        found.append(('\n'.join(part) + ('\n' if at < last or not final else ''), len(part)))
    return found


class Document:
    __slots__ = ('blocks', 'keys', 'line0', 'char0', 'lines', 'size', 'serial', 'caret', 'anchor', 'goal',
                 'version', 'changes', 'log', 'dirty', 'low', 'joined', 'whole', 'record')

    def __init__(self, text=''):
        self.version, self.serial = 0, 0
        # Undo hook: `record(low, old, new, before, after)` for every `replace` (the session's, see history.py).
        self.record = None
        # For `Editor.sync`: the splices since it last ran, the blocks they
        # rewrote and the lowest index among those.
        self.changes, self.log, self.dirty, self.low = [], [], [], 0
        self.caret = self.anchor = 0
        self.load(text)

    def load(self, text):
        """Start over from `text` (the first text; later ones are edits)."""
        self.keys = {}
        self.blocks = [self.block(part, count) for part, count in chunks(text, True)]
        self.line0, self.char0 = [0], [0]
        self.lines, self.size = text.count('\n') + 1, len(text)
        self.whole, self.joined = text, self.version + 1
        self.caret = self.anchor = min(self.caret, self.size)
        self.goal = None
        self.version += 1
        self.log.clear()

    def block(self, text, count):
        self.serial += 1
        found = self.keys[self.serial] = Block(self.serial, text, count)
        return found

    @property
    def text(self):
        """The whole text as one string."""
        if self.joined != self.version:
            self.whole, self.joined = ''.join([block.text for block in self.blocks]), self.version
        return self.whole

    # --- blocks ----------------------------------------------------------
    def grow(self):
        """Extend the base tables by one block."""
        block = self.blocks[len(self.line0) - 1]
        self.line0.append(self.line0[-1] + block.count)
        self.char0.append(self.char0[-1] + len(block.text))

    def at_line(self, line):
        """Index of the block holding `line`."""
        line0, blocks = self.line0, self.blocks
        while len(line0) < len(blocks) and line0[-1] + blocks[len(line0) - 1].count <= line:
            self.grow()
        return bisect_right(line0, line) - 1

    def at_char(self, index):
        """Index of the block holding offset `index`."""
        char0, blocks = self.char0, self.blocks
        while len(char0) < len(blocks) and char0[-1] + len(blocks[len(char0) - 1].text) <= index:
            self.grow()
        return bisect_right(char0, index) - 1

    def first(self, at):
        """The document line of block `at`'s first line."""
        while len(self.line0) <= at:
            self.grow()
        return self.line0[at]

    # --- lines -----------------------------------------------------------
    def line_of(self, index):
        return self.place(index)[0]

    def start(self, line):
        at = self.at_line(line)
        return self.char0[at] + self.blocks[at].starts[line - self.line0[at]]

    def end(self, line):
        """Offset of the line's newline (or the text end)."""
        return self.start(line + 1) - 1 if line + 1 < self.lines else self.size

    def line(self, line):
        at = self.at_line(line)
        block, local = self.blocks[at], line - self.line0[at]
        starts = block.starts
        if local + 1 < len(starts):
            return block.text[starts[local]:starts[local + 1] - 1]
        return block.text[starts[local]:].rstrip('\n')

    def place(self, index):
        """(line, column) of an offset."""
        at = self.at_char(index)
        starts, local = self.blocks[at].starts, index - self.char0[at]
        line = bisect_right(starts, local) - 1
        return self.line0[at] + line, local - starts[line]

    def index(self, line, column):
        line = min(max(line, 0), self.lines - 1)
        return min(self.start(line) + max(column, 0), self.end(line))

    def slice(self, low, high):
        """The text between two offsets."""
        if high <= low:
            return ''
        a, b = self.at_char(low), self.at_char(high)
        low -= self.char0[a]
        high -= self.char0[a]
        if a == b:
            return self.blocks[a].text[low:high]
        return ''.join([block.text for block in self.blocks[a:b + 1]])[low:high]

    def near(self, index):
        """(text, base): the text around offset `index`, from at least
        `NEAR` characters before it to the end of the next block, and the
        offset it starts at. What a helper that scans around the caret gets."""
        at = low = self.at_char(index)
        while low and index - self.char0[low] < NEAR:
            low -= 1
        return ''.join([block.text for block in self.blocks[low:at + 2]]), self.char0[low]

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
        """The raw edit, without an undo step: rewrite the blocks from `low` to
        `high`. Their first block keeps its identity, so its view repaints and
        the blocks below only move."""
        blocks = self.blocks
        a = self.at_char(low)
        b = a if high == low else self.at_char(high)
        base, line = self.char0[a], self.line0[a]
        text = blocks[a].text if a == b else ''.join([block.text for block in blocks[a:b + 1]])
        low -= base
        high -= base
        head = text.rfind('\n', 0, low) + 1
        gone, came = text.count('\n', low, high), new.count('\n')
        change = Splice(line + text.count('\n', 0, head), low - head, gone,
                        high - (text.rfind('\n', 0, high) + 1), came,
                        len(new) - new.rfind('\n') - 1 if came else low - head + len(new))
        parts = chunks(text[:low] + new + text[high:], b == len(blocks) - 1)
        first = blocks[a]
        first.set(*parts[0])
        for block in blocks[a + 1:b + 1]:
            del self.keys[block.key]
        blocks[a + 1:b + 1] = [self.block(part, count) for part, count in parts[1:]]
        del self.line0[a + 1:], self.char0[a + 1:]
        self.dirty.append(first)
        self.low = min(self.low, a)
        self.lines += came - gone
        self.size += len(new) - (high - low)
        self.version += 1
        self.changes.append(change)
        self.log.append(change)
        if len(self.log) > LOG * 2:
            del self.log[:LOG]
        return change

    def since(self, version):
        """The splices made after `version`, or None when they are no longer kept."""
        count = self.version - version
        return self.log[len(self.log) - count:] if 0 <= count <= len(self.log) else None
