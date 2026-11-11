"""One editing session: the document plus everything derived from it.

`Editor.sync` is the only place derived state follows an edit: lexer state,
fold ranges, the line to row mapping and the block stamps that tell the view
which rows to paint again. Anything may edit `doc`; call `sync` afterwards.
"""
from bisect import bisect_left, bisect_right

from meltygui.editor.document import Document
from meltygui.editor.rows import Rows, Folds
from meltygui.editor.suggest import Suggest
from meltygui.editor.syntax import Syntax, structure

# Lever: rows painted by one block view. Smaller blocks repaint less per
# keystroke but cost one declaration each on every edit.
BLOCK = 32


class Editor:
    def __init__(self, text='', language='python'):
        self.doc = Document(text)
        self.syntax = Syntax(language)
        self.folds = Folds()
        self.fold_defaults = True
        self.extra = ()            # caller fold ranges, e.g. unchanged diff context
        self.rows = Rows(self.doc.lines)
        self.stamps = []
        self.drawn = []            # the stamp each block was last declared with
        self.window = (0, 0)       # blocks in or next to the viewport
        self.tick = 0
        self.scopes = []           # (head, last, indent, depth, is_scope), by head
        self.scanned = False       # fold ranges exist for the current text
        self.given = text          # the caller's last input value
        self.selection = (0, 0)    # as last painted
        self.scroll_x = 0.
        self.focused = False
        # Geometry in pixels, measured by the view under the code font.
        self.cw, self.lh, self.gutter, self.digits = 8., 18., 0., 0
        # Marks by 0 based line, prepared by the view from its parameters.
        self.errors, self.fixes, self.live, self.washes = {}, {}, {}, {}
        self.tinted = []           # (head, last, indent, rgb) definition blocks
        self.hits, self.hit_starts = [], []
        self.sources = {}          # parameter name -> the object the marks were built from
        # View state: the suggestion list, what the pointer holds, the open
        # error detail, a clicked popover row, and the native states of the
        # pane, caret and line band (moved by writing their geometry).
        self.suggest = Suggest()
        self.hold = self.detail = self.picked = self.scroll_to = None
        self.pane = self.caret = self.band = None
        self.returned = self.doc.version
        self.layout(0)

    # --- stamps ----------------------------------------------------------
    def touch(self, row=0, last=None):
        """Mark rows `row .. last` (default: to the end) for repainting."""
        self.tick += 1
        stamps, tick = self.stamps, self.tick
        stop = len(stamps) if last is None else min(last // BLOCK + 1, len(stamps))
        for block in range(max(row, 0) // BLOCK, stop):
            stamps[block] = tick

    def look(self, top, height):
        """The viewport moved to `top`. Blocks outside it keep their old
        picture until they come near: bumps the tick when one now needs paint."""
        span = BLOCK * self.lh
        low, high = max(int(top // span) - 1, 0), int((top + height) // span) + 1
        self.window = (low, high)
        if self.drawn[low:high + 1] != self.stamps[low:high + 1]:
            self.tick += 1

    def touch_lines(self, line, last):
        self.touch(self.rows.row(line), self.rows.row(last))

    def layout(self, line):
        """Rebuild the row mapping; rows from `line` on repaint."""
        self.rows = Rows(self.doc.lines, self.folds.closed())
        blocks = -(-self.rows.count // BLOCK)
        del self.stamps[blocks:]
        self.stamps.extend([0] * (blocks - len(self.stamps)))
        del self.drawn[blocks:]
        self.drawn.extend([-1] * (blocks - len(self.drawn)))
        self.touch(self.rows.row(line))

    # --- following edits -------------------------------------------------
    def scan(self):
        """Read fold ranges and scopes from the text. True if they changed."""
        doc, folds = self.doc, self.folds
        ranges, keys, defaults = self.syntax.folds(doc.text)
        for item in self.extra:
            item = tuple(item)
            ranges.append(item)
            keys[item] = ('extra',) + item
            defaults.append(item)
        before = folds.keys
        folds.set(ranges, keys, defaults if self.fold_defaults else ())
        self.scanned = self.syntax.live
        if keys == before:
            return False
        scopes, open_ends = [], []
        for head, last in folds.ranges:
            kind = keys[(head, last)][0]
            if kind not in ('scope', 'block'):
                continue
            while open_ends and open_ends[-1] < head:
                open_ends.pop()
            line = doc.line(head)
            scopes.append((head, last, len(line) - len(line.lstrip(' ')), len(open_ends), kind == 'scope'))
            open_ends.append(last)
        self.scopes = scopes
        return True

    def sync(self):
        """Bring lexer state, folds, rows and stamps up to the document.
        Returns True when the text changed since the last call."""
        doc = self.doc
        changes = doc.changes
        if not changes:
            if not self.scanned and self.syntax.live:
                # The lexer finished loading: colours and folds appear.
                self.syntax.update(doc.text)
                self.scan()
                self.layout(0)
            return False
        first = min(change.line for change in changes)
        last = max(change.line + change.added - 1 for change in changes)
        shifted = any(change.removed != change.added for change in changes)
        only = changes[0] if len(changes) == 1 else None
        recolour = self.syntax.update(doc.text, only and (only.line + only.removed, only.line + only.added))
        rescan = shifted or recolour is not None or not self.scanned
        if not rescan:
            for change in changes:
                olds = change.old.split('\n')
                if any(structure(old) != structure(doc.line(change.line + i)) for i, old in enumerate(olds)):
                    rescan = True
                    break
        changes.clear()
        above = self.heads(first) if rescan else None
        refolded = rescan and self.scan()
        if shifted or refolded:
            # Rows above the edit keep their pixels unless a fold there changed.
            self.layout(first if not refolded or self.heads(first) == above else 0)
        else:
            self.touch_lines(first, last)
            if recolour is not None:
                self.touch(self.rows.row(recolour))
        self.show(doc.caret)
        return True

    def heads(self, line):
        """What the rows above `line` show of the folds: each head, its key,
        whether it is collapsed."""
        folds = self.folds
        return [(item[0], folds.keys[item], folds.is_closed(item)) for item in folds.ranges if item[0] < line]

    def load(self, text):
        """The caller replaced the text."""
        self.doc.load(text)
        self.doc.changes.clear()
        self.syntax.update(text)
        self.scan()
        self.layout(0)

    # --- folds -----------------------------------------------------------
    def show(self, index):
        """Unfold whatever hides the line of `index`."""
        line = self.doc.line_of(index)
        if self.rows.hidden(line) and self.folds.reveal(line):
            self.layout(0)

    def toggle(self, line):
        """Collapse or expand the fold headed by `line`."""
        if not self.folds.toggle(line):
            return False
        self.layout(line)
        self.settle()
        return True

    def fold_all(self, closed):
        self.folds.set_all(closed)
        self.layout(0)
        self.settle()

    def settle(self):
        """A caret left inside hidden lines moves to its fold header's end."""
        doc, rows = self.doc, self.rows
        if rows.hidden(doc.line_of(doc.caret)) or rows.hidden(doc.line_of(doc.anchor)):
            doc.select(doc.end(rows.line(rows.row(doc.line_of(doc.caret)))))

    # --- geometry --------------------------------------------------------
    def spot(self, x, y, scroll_y):
        """(line, column) under a point of the view."""
        row = int((y + scroll_y) // self.lh)
        line = self.rows.line(row)
        column = int((x - self.gutter + self.scroll_x) / self.cw + .5)
        return line, max(0, column)

    def where(self, index):
        """(x, y) of an offset in content pixels, before horizontal scroll."""
        line, column = self.doc.place(index)
        return self.gutter + column * self.cw, self.rows.row(line) * self.lh

    # --- search ----------------------------------------------------------
    def find(self, term):
        """Offsets of `term` (case blind), kept sorted for per line lookup."""
        hits = []
        if term:
            text, needle, at = self.doc.text.lower(), term.lower(), 0
            while True:
                at = text.find(needle, at)
                if at < 0:
                    break
                hits.append((at, at + len(needle)))
                at += len(needle)
        self.hits, self.hit_starts = hits, [start for start, _ in hits]

    def hits_in(self, start, stop):
        hits = self.hits
        if not hits:
            return ()
        return hits[max(bisect_left(self.hit_starts, start) - 1, 0):bisect_right(self.hit_starts, stop)]
