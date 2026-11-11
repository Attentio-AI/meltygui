"""One editing session: the document plus everything derived from it.

`Editor.sync` is the only place derived state follows an edit, and it only
does work the edit's own blocks ask for: their stamps and lexer state, and one
`follow` on every fold layer and mark set. What needs the whole text (fold
ranges, search hits) is asked of the session's `Worker` and arrives through
`Editor.land` on a later run, brought forward over the edits made meanwhile.

`Editor.marks` holds every decoration by producer name (`editor/marks.py`).
A new kind of decoration is a new set: `Editor.put(name, marks)`.

`Editor.window` decides which blocks the view declares: the ones near the
viewport. Anything may edit `doc`; call `sync` afterwards.
"""
import re
import sys
import time

from meltygui.editor import inline, marks
from meltygui.editor.document import Document, BLOCK
from meltygui.editor.rows import Rows, Folds, Numbers, nest
from meltygui.editor.suggest import Suggest, Menu
from meltygui.editor.syntax import Syntax, outline, structure, shift, unshift
from meltygui.editor.worker import Worker

# Levers, in rows: how far past the viewport blocks stay declared, and how
# close the viewport may come to the end of them before they are chosen again.
REACH = BLOCK * 3
EDGE = BLOCK // 2
# Lever: texts up to this many characters get their first fold scan and
# their searches at once; larger ones on the worker.
INLINE = 60000
# Parameter types compared by value; anything else by identity.
PLAIN = (int, float, str, bool, frozenset)
# The built in mark sets, in paint order within a layer; sets a caller adds come after.
ORDER = ('tints', 'paused', 'hits', 'flash', 'live', 'errors', 'breaks')
# Lever: seconds the text an undo or a jump landed on stays flashed.
FLASH = 0.6


def scan(text, version):
    return version, outline(text)


def search(text, term, version):
    return (marks.hits(text, term, version),)


class Pane:
    """A caller's handle on a running editor: commands in, geometry out.
    Reach it as `EditorState.pane`. Commands are run by the view on its next
    run (it is woken); geometry is as of its last run, in the view's own
    coordinates. Lines are file lines, as everywhere in the caller's API.

        state.pane.jump(120, token='total')      # unfold, centre, flash
        y = state.pane.y(120)                    # for an overlay beside the editor
    """
    __slots__ = ('ed',)

    def __init__(self, ed):
        self.ed = ed

    def ask(self, *command):
        self.ed.asks.append(command)
        if self.ed.wake is not None:
            self.ed.wake()

    # --- commands --------------------------------------------------------
    def jump(self, line, token=None, column=0, select=False, flash=True):
        """Put the caret on file line `line`: at `column`, or on `token`
        where the line has it (selected with `select`). Whatever folds hide
        the line open, it is centred when out of sight, and flashed."""
        self.ask('jump', line, token, column, select, flash)

    def select(self, caret, anchor=None):
        """Set the caret (and the other end of the selection) by offset."""
        self.ask('select', caret, anchor)

    def focus(self):
        """Give the editor the keyboard."""
        self.ask('focus')

    # --- geometry --------------------------------------------------------
    @property
    def version(self):
        """The document version: tag results computed for the text with it (`Marks.version`)."""
        return self.ed.doc.version

    @property
    def line_height(self):
        return self.ed.lh

    @property
    def char_width(self):
        return self.ed.cw

    @property
    def scroll(self):
        """(x, y) scrolled, in pixels."""
        return self.ed.scroll_x, self.ed.pane.scroll_offset[1] if self.ed.pane is not None else 0.

    @property
    def origin(self):
        """Where column 0 of row 0 is, relative to the view's corner."""
        x, y = self.scroll
        return self.ed.gutter - x, -y

    @property
    def rows(self):
        """Rows in all: the lines no fold hides."""
        return self.ed.rows.count

    def row(self, line):
        """The row showing file line `line` (its fold's first line when hidden), or None."""
        line = self.ed.numbers.line(line)
        return self.ed.rows.row(line) if line is not None and 0 <= line < self.ed.doc.lines else None

    def line(self, row):
        """The file line on `row`."""
        return self.ed.numbers.number(self.ed.rows.line(row))

    def y(self, line):
        """Top of file line `line` relative to the view's corner, or None."""
        row = self.row(line)
        return row * self.ed.lh - self.scroll[1] if row is not None else None


class Editor:
    def __init__(self, text='', language='python', fold_defaults=True, collapsed=(), gaps=()):
        """`collapsed`: the scope fold names a previous session left
        collapsed; `gaps`: the caller ranges it left collapsed."""
        self.doc = Document(text)
        self.doc.record = self.record
        self.syntax = Syntax(language)
        self.worker = Worker()
        # Undo: `step` is the framework undo step the running command is
        # adding to (history.py); `dragging` while an inline widget edits its
        # token; `landing` (low, high, flash) is what an undo or a jump wants shown.
        self.step = self.landing = self.wake = self.path = None
        self.dragging = self.closed = False
        self.flash = 0.            # when the 'flash' marks expire (0: none shown)
        # Fold layers, lowest first: scopes read from the text, then the
        # caller's ranges (unchanged diff context). Each keeps its own
        # collapsed set; one `Rows` covers all of them. A further layer is
        # another `Folds` in `layers`.
        self.folds, self.extra = Folds(), Folds()
        self.layers = (self.folds, self.extra)
        self.fold_defaults = fold_defaults
        self.folds.collapsed = {tuple(name) for name in collapsed}
        self.folds.seeded = bool(collapsed)
        self.extra.collapsed = {('extra',) + tuple(item) for item in gaps}
        self.standin = False       # the text is a viewport snapshot, shown until the real one arrives
        self.rows = Rows(self.doc.lines)
        self.numbers = Numbers()   # buffer lines to file lines
        self.look = None           # the colour table (view/editor_view.py), as last painted
        self.handle = Pane(self)   # what callers may reach: `EditorState.pane`
        self.asks = []             # commands from the handle, run by the view
        # Repainting: a block view runs again when its stamp or `base` rises.
        self.tick = self.base = 0
        self.shown = []            # (block, first line, first row, rows): the declared blocks
        self.placed = {}           # block key -> (first line, first row), for its view
        self.held = (0, -1)        # the rows `shown` was chosen for
        self.coloured = False      # the lexer has been seen loaded
        self.settling = False      # string state is still catching up with the viewport
        self.given = self.gave = text      # the caller's last input value, the view's last return
        self.shape = None          # (line, structure) of the caret's line, as `rest` left it
        self.inside = None         # (line, column) of the widget token the caret is in
        self.selection = (0, 0)    # as last painted
        self.scroll_x = 0.
        self.focused = False
        # Geometry in pixels, measured by the view under the code font.
        self.cw, self.lh, self.gutter = 8., 18., 0.
        # Decorations: set name -> `Marks` (None: nothing), in paint order.
        self.marks = dict.fromkeys(ORDER)
        self.sources = {}          # set name -> the values it was built from
        self.spots = {}            # block key -> [(row, x0, x1, action, line)]: what a click hits besides text
        self.slots = {}            # gutter slot name -> action(line) for a click on it
        self.term = ''
        # View state: the suggestion list, what the pointer holds, the open
        # error detail, a clicked popover row, and the native states of the
        # pane, caret and line band (moved by writing their geometry).
        self.suggest = Suggest()
        self.fixes = Menu()        # the quick fixes of the open error detail
        self.hold = self.detail = self.scroll_to = None
        self.creep = (0., 0.)      # rows owed to a selection held past an edge, and when it last moved
        self.pane = self.caret = self.band = None
        self.returned = self.doc.version
        self.kept = -1             # the tick the viewport snapshot was last kept at
        self.outline(now=self.doc.size <= INLINE)
        self.land()

    # --- stamps ----------------------------------------------------------
    def touch(self):
        """Every declared block repaints."""
        self.tick += 1
        self.base = self.tick

    def touch_lines(self, line, last):
        """The blocks holding lines `line .. last` repaint."""
        doc = self.doc
        low, high = doc.at_line(line), doc.at_line(last)
        if high - low > len(self.shown):
            return self.touch()
        self.tick += 1
        for block in doc.blocks[low:high + 1]:
            block.stamp = self.tick

    def window(self, top, height):
        """The viewport is at `top`: choose the blocks to declare and settle
        their string state. Bumps the tick when the view has to run again."""
        doc, rows, lh = self.doc, self.rows, self.lh
        low, high = int(top // lh), int((top + height) // lh)
        if not (self.held[0] <= max(low - EDGE, 0) and high + EDGE <= self.held[1]):
            self.held = (low - REACH, high + REACH)
        first, last = doc.at_line(rows.line(self.held[0])), doc.at_line(rows.line(self.held[1]))
        if self.syntax.live:
            if not self.coloured:
                self.coloured = True
                self.touch()
            changed, done = self.syntax.reach(doc, last)
            self.settling = not done
            if self.syntax.spilled:
                # A string opened or closed over the lines below: the folds changed too.
                self.syntax.spilled = False
                self.outline(rest=True)
            if changed:
                self.tick += 1
                for block in changed:
                    block.stamp = self.tick
        shown = []
        for at in range(first, last + 1):
            block, line = doc.blocks[at], doc.first(at)
            row = rows.before(line)
            count = rows.before(line + block.count) - row
            if count:
                shown.append((block, line, row, count))
        if shown != self.shown:
            self.shown = shown
            self.placed = {block.key: (line, row) for block, line, row, _ in shown}
            self.spots = {key: found for key, found in self.spots.items() if key in self.placed}
            self.tick += 1

    def layout(self):
        """Rebuild the row mapping from the collapsed ranges of every layer."""
        self.rows = Rows(self.doc.lines, [item for layer in self.layers for item in layer.ranges()])

    # --- following edits -------------------------------------------------
    def sync(self):
        """Bring everything derived up to the document. Returns True when
        the text changed since the last call."""
        doc = self.doc
        changes = doc.changes
        if not changes:
            return False
        shifted = False
        # Typing inside a line that keeps its structure cannot change the folds.
        only = changes[0]
        calm = (len(changes) == 1 and not (only.gone or only.came)
                and self.shape == (only.line, structure(doc.line(only.line))))
        for change in changes:
            for layer in self.layers:
                layer.follow(change)
            for found in self.marks.values():
                if found is not None:
                    found.follow(change)
            shifted |= bool(change.gone or change.came)
        changes.clear()
        self.tick += 1
        for block in doc.dirty:
            block.stamp = self.tick
        doc.dirty.clear()
        self.syntax.edited(doc.low)
        doc.low = len(doc.blocks)      # nothing rewritten since
        if shifted:
            self.layout()
        if not calm:
            self.outline(rest=True)
        if self.term:
            self.worker.ask('hits', search, doc.text, self.term, doc.version, rest=True)
        self.show(doc.caret)
        return True

    def rest(self):
        """The run is over: remember the caret line's structure for the next edit."""
        line = self.doc.line_of(self.doc.caret)
        self.shape = (line, structure(self.doc.line(line)))

    def outline(self, rest=False, now=False):
        """Ask for the fold ranges of the current text."""
        if self.syntax.language == 'python':
            self.worker.ask('folds', scan, self.doc.text, self.doc.version, rest=rest, now=now and self.syntax.live)

    def land(self):
        """Take what the worker finished. Returns True when anything arrived."""
        found = self.worker.take()
        for kind, result in found:
            getattr(self, 'land_' + kind)(*result)
        return bool(found)

    def land_folds(self, version, found):
        changes = self.doc.since(version)
        if changes is None:
            return self.outline()
        heads, spans, parents, names, scopes, defaults = found
        if not changes and self.folds.same(heads, spans, names):
            return
        self.folds.set(heads, spans, parents, names, scopes, defaults if self.fold_defaults else ())
        for change in changes:
            self.folds.follow(change)
        self.layout()
        self.settle()
        self.touch()

    def land_hits(self, found):
        self.put('hits', found)

    # --- marks -----------------------------------------------------------
    def put(self, name, found):
        """Show `found` (a `Marks`; None clears) as the set called `name`.
        A set computed for an older version is brought forward first; one too
        old to follow is dropped and the set it would replace stays."""
        if found is not None:
            found = marks.current(found, self.doc)
            if found is None:
                return
        if found is not None or self.marks.get(name) is not None:
            self.marks[name] = found
            self.touch()

    def changed(self, name, values):
        """Whether `values` (a tuple) differ from the ones last seen under
        `name`: objects by identity, plain values by value."""
        old = self.sources.get(name)
        if old is not None and len(old) == len(values) and all(
                new is was or (type(new) in PLAIN and new == was) for new, was in zip(values, old)):
            return False
        self.sources[name] = values
        return True

    def produce(self, name, values, build):
        """Rebuild the set `name` with `build(*values)` when `values` changed."""
        if self.changed(name, values):
            self.put(name, build(*values))

    def mark(self, name, line, shape):
        """The first `shape` mark of the set `name` on `line`, or None."""
        found = self.marks.get(name)
        return next((mark for _, mark in found.on(line) if mark.shape == shape), None) if found is not None else None

    def reveal(self, low, high, top, height, flash=True):
        """Show the text from offset `low` to `high`: unfold what hides it
        and flash it. Returns the scroll offset that centres it when it is
        not inside the viewport (`top`, `height`), else None."""
        doc, lh = self.doc, self.lh
        low, high = min(low, doc.size), min(max(high, low), doc.size)
        self.show(low)
        self.show(high)
        (first, column), (last, stop) = doc.place(low), doc.place(high)
        if flash:
            found = [(first, marks.Mark('flash', column, max(stop, column + 1)))] if first == last else [
                (line, marks.Mark('flash')) for line in range(first, min(last, first + 200) + 1)]
            self.put('flash', marks.Marks(found))
            self.flash = time.monotonic() + FLASH
        y0, y1 = self.rows.row(first) * lh, (self.rows.row(last) + 1) * lh
        if y0 >= top and y1 <= top + height:
            return None
        return max(y0 - (height - min(y1 - y0, 6 * lh)) / 2, 0.)

    def jump(self, number, token=None, column=0, select=False):
        """Move the caret to file line `number` (see `Pane.jump`). Returns
        the (low, high) offsets to reveal, or None when the line is not here."""
        doc = self.doc
        line = self.numbers.line(number)
        if line is None or not 0 <= line < doc.lines:
            return None
        text, length = doc.line(line), 0
        if token:
            # The token as a whole word, else wherever it is spelled, else the line's start.
            found = next((match.start() for match in re.finditer(re.escape(token), text)
                          if not (text[:match.start()][-1:] + text[match.end():][:1]).replace('_', 'a').isalnum()), text.find(token))
            column, length = (found, len(token)) if found >= 0 else (len(text) - len(text.lstrip()), 0)
        low = doc.index(line, column)
        doc.select(low + length if select else low, low if select else None)
        return low, low + length

    def find(self, term):
        """Highlight every occurrence of `term` (case blind)."""
        self.term = term or ''
        self.put('hits', None)
        if term:
            self.worker.ask('hits', search, self.doc.text, term, self.doc.version, now=self.doc.size <= INLINE)
            self.land()

    def ranges(self, found):
        """Set the caller's fold layer from `(head, last)` line ranges. A
        range starts collapsed, unless it overlaps one the user expanded:
        the ranges come from a live diff and drift as the text changes."""
        layer = self.extra
        opened = [layer.range(at) for at in range(len(layer)) if layer.names[at] is not None and not layer.is_closed(at)]
        heads, spans, parents, found = nest(tuple(item) for item in found or ())
        names = [('extra',) + item for item in found]
        first = not len(layer)
        layer.set(heads, spans, parents, names)
        if not (first and layer.collapsed):
            # (On the first set the collapsed ranges are the ones restored from the last session.)
            layer.collapsed.clear()
            layer.close(name for name in names
                        if not any(head <= name[2] and name[1] <= last for head, last in opened))
        self.layout()
        self.settle()
        self.touch()

    def load(self, text):
        """The caller replaced the text: apply the difference as one splice.
        It is not an undo step (whoever changed the text owns that); the
        caret stays on its text and this editor's own steps follow the change."""
        from meltygui.editor.edit import differ
        doc = self.doc
        old = doc.text
        head, tail = differ(old, text)
        low, high, new = head, len(old) - tail, text[head:len(text) - tail]
        if low == high and not new:
            return
        caret, anchor = (at + len(new) - (high - low) if at >= high else min(at, low + len(new))
                         for at in (doc.caret, doc.anchor))
        doc.splice(low, high, new)
        doc.select(caret, anchor)
        self.step = None
        if 'meltygui.editor.history' in sys.modules:
            sys.modules['meltygui.editor.history'].rebase(self, low, high, len(new))
        self.sync()

    # --- undo ------------------------------------------------------------
    def record(self, low, old, new, before, after):
        """The document's undo hook: the edit goes on the framework's stack."""
        from meltygui.editor import history
        history.record(self, low, old, new, before, after)

    def replay(self, step, undo):
        """Undo or redo one of this session's steps. Called by the
        framework's undo stack, at any time between runs: the view is woken
        to follow, restore the caret and show where the text changed."""
        if self.closed:
            return
        doc = self.doc
        low, high = doc.size, 0
        for at, old, new in (reversed(step.parts) if undo else step.parts):
            gone, came = (new, old) if undo else (old, new)
            doc.splice(at, at + len(gone), came)
            low, high = min(low, at), max(high, at + len(came))
        doc.select(*(step.before if undo else step.after))
        step.version = doc.version
        self.step, self.landing = None, (low, max(high, low), True)
        if self.wake is not None:
            self.wake()

    # --- folds -----------------------------------------------------------
    def fold(self, line):
        """(layer, index) of the fold headed by `line`, or None."""
        for layer in self.layers:
            at = layer.at(line)
            if at is not None:
                return layer, at
        return None

    def show(self, index):
        """Unfold whatever hides the line of `index`."""
        line = self.doc.line_of(index)
        if self.rows.hidden(line) and any([layer.reveal(line) for layer in self.layers]):
            self.layout()
            self.touch()

    def toggle(self, line):
        """Collapse or expand the fold headed by `line`."""
        found = self.fold(line)
        if found is None:
            return False
        found[0].flip(found[1])
        self.layout()
        self.settle()
        self.touch()
        return True

    def fold_all(self, closed):
        self.folds.set_all(closed)
        self.layout()
        self.settle()
        self.touch()

    def snapshot(self):
        """(first row, rows in all, text, file numbers) of the declared
        blocks as they are shown: what `EditorState` keeps so that a later
        session can paint before its text is loaded."""
        doc, rows, numbers = self.doc, self.rows, self.numbers
        if not self.shown:
            return 0, rows.count, '', []
        if rows.first:
            lines = [line for block, first, _, _ in self.shown for line in range(first, first + block.count)
                     if not rows.hidden(line)]
            text = '\n'.join([doc.line(line) for line in lines])
        else:
            lines = range(self.shown[0][1], self.shown[-1][1] + self.shown[-1][0].count)
            text = ''.join([block.text for block, *_ in self.shown])
            text = text[:-1] if text.endswith('\n') and lines[-1] + 1 < doc.lines else text
        return self.shown[0][2], rows.count, text, [numbers.number(line) for line in lines]

    def settle(self):
        """A caret left inside hidden lines moves to its fold header's end."""
        doc, rows = self.doc, self.rows
        if rows.hidden(doc.line_of(doc.caret)) or rows.hidden(doc.line_of(doc.anchor)):
            doc.select(doc.end(rows.line(rows.row(doc.line_of(doc.caret)))))

    # --- geometry --------------------------------------------------------
    # The column map: `x` and `column` are the only two places that turn a
    # column into a position and back. Everything that places or hit-tests
    # text goes through them (or, per painted line, through its `insets`).
    def insets(self, line):
        """The cells inline widgets add to `line` (`Syntax.insets`), or None."""
        doc = self.doc
        at = doc.at_line(line)
        states = doc.blocks[at].open
        return self.syntax.insets(doc.line(line), states[line - doc.line0[at]] if states else None)

    def x(self, line, column):
        """Content x of `column` on `line`, before horizontal scroll."""
        inset = self.insets(line)
        return self.gutter + (column + shift(inset, column) if inset else column) * self.cw

    def column(self, line, x):
        """The column on `line` whose caret is nearest content x."""
        cell = (x - self.gutter) / self.cw
        inset = self.insets(line)
        return unshift(inset, cell) if inset else max(int(cell + .5), 0)

    def spot(self, x, y, scroll_y):
        """(line, column) under a point of the view."""
        line = self.rows.line(int((y + scroll_y) // self.lh))
        return line, self.column(line, x + self.scroll_x)

    def where(self, index):
        """(x, y) of an offset in content pixels, before horizontal scroll."""
        line, column = self.doc.place(index)
        return self.x(line, column), self.rows.row(line) * self.lh

    def widget(self, line, x):
        """(entry, column, length) of the inline widget under content x on
        `line`, or None. A widget that is its token is not there while the
        caret is inside the token."""
        doc, cw = self.doc, self.cw
        text = doc.line(line)
        inset = self.insets(line)
        for start, piece, kind in self.syntax.runs(text, self.syntax.state(doc, line)):
            entry = inline.for_token(kind)
            if entry is None:
                continue
            left = self.gutter + (start + shift(inset, start) if inset else start) * cw
            if entry.lead:
                low, high = left - entry.lead * cw, left
            elif entry.trail:
                low, high = left + len(piece) * cw, left + (len(piece) + entry.trail) * cw
            elif self.inside == (line, start):
                continue
            else:
                low, high = left, left + len(piece) * cw
            if low <= x < high:
                return entry, start, len(piece)
        return None

    def enter(self):
        """Note which widget token the caret is inside: that widget steps
        aside, so the lines of the old and the new one repaint."""
        doc = self.doc
        line, column = doc.place(doc.caret)
        found = None
        if self.focused:
            for start, piece, kind in self.syntax.runs(doc.line(line), self.syntax.state(doc, line)):
                entry = inline.for_token(kind)
                if start <= column <= start + len(piece) and entry is not None and not (entry.lead or entry.trail):
                    found = (line, start)
        if found != self.inside:
            for item in (found, self.inside):
                if item is not None and item[0] < doc.lines:
                    self.touch_lines(item[0], item[0])
            self.inside = found
