"""One editing session: the document plus everything derived from it.

`Editor.sync` is the only place derived state follows an edit, and it only
does work the edit's own blocks ask for: their stamps and lexer state, and one
`follow` on the rows and every mark set. What needs the whole text (fold
ranges, search hits) is asked of the session's `Source` (model/source_model.py:
the text mirrored in Rust, read off the render thread) and arrives through
`Editor.land` on a later run, brought forward over the edits made meanwhile.
It stays in Rust: `Editor.rows` (`Rows`: lines to displayed rows and the folds
between them) and the search hits answer for the lines asked about, so
nothing here grows with the file.

`Editor.marks` holds every decoration by producer name (`editor/marks.py`).
A new kind of decoration is a new set: `Editor.put(name, marks)`.

`Editor.window` decides which blocks the view declares: the ones near the
viewport. Anything may edit `doc`; call `sync` afterwards.

Several sessions may be on one `Source` (two panes on a file, a compare
view), and code that is no editor may splice it too. The text, its version
and the log of splices are the source's; a session follows the splices it
did not make in `sync` (rows, marks, and its caret stays on its character)
and takes its own folds and search hits out of every reading (`land`).
"""
import re
import sys
import time

from meltygui.editor import inline, marks
from meltygui.editor.document import Document, BLOCK
from meltygui.editor.find import Find
from meltygui.editor.lookup import Lookup
from meltygui.editor.rows import Rows, Numbers
from meltygui.editor.suggest import Suggest, Menu
from meltygui.editor.syntax import Syntax, structure, shift, unshift

# Levers, in rows: how far past the viewport blocks stay declared, and how
# close the viewport may come to the end of them before they are chosen again.
REACH = BLOCK * 3
EDGE = BLOCK // 2
# Lever: texts up to this many characters get their first fold scan and
# their searches at once; larger ones on the source's thread.
INLINE = 60000
# Lever: seconds the text must rest before the source reads it again: a
# burst of typing is read once.
QUIET = 0.15
# Parameter types compared by value; anything else by identity.
PLAIN = (int, float, str, bool, frozenset)
# The built in mark sets, in paint order within a layer; sets a caller adds come after.
ORDER = ('tints', 'paused', 'hits', 'found', 'flash', 'live', 'errors', 'breaks')
# Lever: seconds the text an undo or a jump landed on stays flashed.
FLASH = 0.6


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

    def find(self, delta=1):
        """Step to the next (`delta` 1) or the previous (-1) hit of the
        search term, around the ends: it is unfolded, brought into view and
        flashed. For a caller with a search field of its own (`search=`)."""
        self.ask('find', delta)

    @property
    def search(self):
        """(term, index, count) of the search (`find.Search`): the current hit counted from 1 (0: none), the hits in all."""
        return self.ed.search.state(self.ed)

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


class Block:
    """What the session keeps of a block it has shown (`Document.blocks`):
    how it was last painted. `edit` is the version of the edit that last
    rewrote its lines, `stamp` the tick it was last asked to repaint at."""
    __slots__ = ('key', 'count', 'edit', 'stamp', 'open', 'wide')

    def __init__(self, key):
        self.key, self.count, self.edit, self.stamp = key, 0, 0, 0
        self.open = None           # the string state its first line was last painted in
        self.wide = 0              # longest line in cells, known once painted


class Editor:
    def __init__(self, text='', language='python', fold_defaults=True, collapsed=(), gaps=(), source=None):
        """`collapsed`: the scope fold names a previous session left
        collapsed; `gaps`: the caller ranges it left collapsed; `source`:
        the caller's `Source` (the view's value): the text is the one it
        holds, edited by this session along with any other on it. Without
        one the session has its own for `text`."""
        self.doc = Document(text, source)
        self.doc.record, self.doc.by = self.record, self
        self.source = self.doc.source
        self.seen = self.doc.version   # the version of the text this session has followed to
        self.took = -1             # the source reading (`Source.stamp`) this session took
        self.keys = None           # the source site keys of this session's breakpoints
        self.syntax = Syntax(language, self.source)
        # Undo: `step` is the framework undo step the running command is
        # adding to (history.py); `dragging` while an inline widget edits its
        # token; `landing` (low, high, flash) is what an undo or a jump wants shown.
        self.step = self.landing = self.wake = self.path = None
        self.waker = None          # the view's (handle, token) for the source's reading thread (`RetainedCache.waker`)
        self.dragging = self.closed = False
        self.flash = 0.            # when the 'flash' marks expire (0: none shown)
        # Lines to rows over two fold layers: the scopes the source reads
        # from the text, then the caller's ranges (unchanged diff context).
        # Each keeps its own collapsed set. Default folds collapse once, on
        # the first reading, unless a session's own were restored.
        self.rows = Rows(self.doc.lines, [tuple(name) for name in collapsed], bool(collapsed) or not fold_defaults,
                         [tuple(item) for item in gaps])
        self.saved = -1            # the `rows.stamp` the collapsed names were last saved at
        self.standin = False       # the text is a viewport snapshot, shown until the real one arrives
        self.numbers = Numbers()   # buffer lines to file lines
        self.look = None           # the colour table (view/editor_view.py), as last painted
        self.handle = Pane(self)   # what callers may reach: `EditorState.pane`
        self.asks = []             # commands from the handle, run by the view
        # Repainting: a block view runs again when its stamp or `base` rises.
        self.tick = self.base = 0
        self.blocks = {}           # block key -> `Block`, of the blocks shown so far
        self.shown = []            # (block, first line, first row, rows): the declared blocks
        self.placed = {}           # block key -> (first line, first row), for its view
        self.held = (0, -1)        # the rows `shown` was chosen for
        self.coloured = False      # the lexer has been seen loaded
        # The caller's last input value and the view's last return (a caller's `Source` is both).
        self.given = self.gave = text if source is None else source
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
        # error detail (`detail`: a line, kept as a mark), a clicked popover row, and the
        # native states of the pane, caret and line band (moved by writing their geometry).
        self.suggest = Suggest()
        self.fixes = Menu()        # the quick fixes of the open error detail
        self.lookup = Lookup()     # Ctrl+B: the lookup under way and the usage picker
        self.search = Find()       # the find bar and the current hit (the hits themselves are the 'hits' set)
        self.hold = self.detail = self.scroll_to = None
        self.creep = (0., 0.)      # rows owed to a selection held past an edge, and when it last moved
        self.pane = self.caret = self.band = None
        self.returned = self.doc.version
        self.kept = -1             # the tick the viewport snapshot was last kept at
        self.breakpoints = None    # the caller's {source site key: state}, toggled in place by gutter clicks
        self.dotted = None         # the keys the breakpoint dots were last drawn for
        self.source.sessions.add(self)
        self.ask(now=self.doc.size <= INLINE)
        self.land()

    def close(self):
        """The session ends. Its undo steps stay: they are the text's."""
        self.closed = True
        self.source.sessions.discard(self)

    # --- stamps ----------------------------------------------------------
    def touch(self):
        """Every declared block repaints."""
        self.tick += 1
        self.base = self.tick

    def touch_lines(self, line, last):
        """The blocks holding lines `line .. last` repaint."""
        if (last - line) // BLOCK > len(self.shown):
            return self.touch()
        self.tick += 1
        for key, *_ in self.doc.blocks(line, last):
            if key in self.blocks:
                self.blocks[key].stamp = self.tick

    def window(self, top, height):
        """The viewport is at `top`: choose the blocks to declare and settle
        their string state. Bumps the tick when the view has to run again."""
        doc, rows, lh = self.doc, self.rows, self.lh
        low, high = int(top // lh), int((top + height) // lh)
        if not (self.held[0] <= max(low - EDGE, 0) and high + EDGE <= self.held[1]):
            self.held = (low - REACH, high + REACH)
        blocks, shown = self.blocks, []
        for key, line, count, edit in doc.blocks(rows.line(self.held[0]), rows.line(self.held[1])):
            block = blocks.get(key)
            if block is None:
                block = blocks[key] = Block(key)
            block.count, block.edit = count, edit
            row = rows.before(line)
            height = rows.before(line + count) - row
            if height:
                shown.append((block, line, row, height))
        if self.syntax.live:
            if not self.coloured:
                self.coloured = True
                self.touch()
            # A block whose first line starts in another string than it was painted in repaints
            # (a quote typed above it; the first reading of the text above it landed).
            for block, line, _, _ in shown:
                state = self.syntax.state(line)
                if state != block.open:
                    block.open = state
                    self.tick += 1
                    block.stamp = self.tick
        if shown != self.shown:
            self.shown = shown
            self.placed = {block.key: (line, row) for block, line, row, _ in shown}
            self.spots = {key: found for key, found in self.spots.items() if key in self.placed}
            self.tick += 1

    # --- following edits -------------------------------------------------
    def sync(self):
        """Bring everything derived up to the text: follow the splices made
        since the last call, this session's and everyone else's. Returns
        True when the text changed since then."""
        doc, source = self.doc, self.source
        if self.seen == doc.version:
            return False
        entries = source.since(self.seen)
        self.seen = doc.version
        if entries is None:
            self.renew()
            return True
        mine = [entry[5] is self for entry in entries]
        last = max((at for at, own in enumerate(mine) if own), default=-1)
        # Typing inside a line that keeps its structure cannot change the folds
        # (it can change what the lint finds: a source that lints is read again).
        only = entries[0][1]
        calm = (len(entries) == 1 and mine[0] and not (only.gone or only.came)
                and self.shape == (only.line, structure(doc.line(only.line))))
        for at, (version, change, low, gone, came, _) in enumerate(entries):
            self.rows.follow(change.line, change.column, change.gone, change.came)
            for found in self.marks.values():
                # (A set another session on the source shows too may have followed already.)
                if found is not None and (found.version is None or found.version < version):
                    found.follow(change)
                    if found.version is not None:
                        found.version = version
            if at > last:
                # Someone else's edit: the caret and the selection stay on their characters.
                doc.caret, doc.anchor = (spot + came - gone if spot >= low + gone else min(spot, low + came)
                                         for spot in (doc.caret, doc.anchor))
        self.tick += 1
        if self.term or not calm or source.lint:
            self.ask(rest=True)
        if last >= 0:
            self.show(doc.caret)
        return True

    def renew(self):
        """The text moved on by more splices than its log keeps (a session
        that did not run for long): start over on the text as it is. The
        folds keep their collapsed names; marks are given again by whoever
        gives them."""
        doc = self.doc
        self.rows = Rows(doc.lines, [tuple(name) for name in self.rows.collapsed()], True, [tuple(item) for item in self.rows.closed(1)])
        self.marks, self.sources = dict.fromkeys(self.marks), {}
        doc.select(doc.caret, doc.anchor)
        self.touch()
        self.read()

    def rest(self):
        """The run is over: remember the caret line's structure for the next edit."""
        line = self.doc.line_of(self.doc.caret)
        self.shape = (line, structure(self.doc.line(line)))

    def ask(self, rest=False, now=False):
        """Ask the source for the facts of the current text: fold ranges and
        the occurrences of the search term. `rest`: once typing rests."""
        if self.term or self.syntax.language == 'python':
            self.source.ask(self.term, QUIET if rest else 0., now, self.rows)

    def read(self):
        """Have the facts read again: at once for a small text, else on the source's thread."""
        small = self.doc.size <= INLINE
        self.ask(now=small)
        if small:
            self.land()

    def back(self, line):
        """Where buffer line `line` was in the text the landed facts were read from."""
        for change in reversed(self.doc.since(self.source.version) or ()):
            if line > change.line + change.came:
                line += change.gone - change.came
            elif line > change.line:
                line = change.line
        return line

    def forth(self, line):
        """Where a line of the text the landed facts were read from is now."""
        for change in self.doc.since(self.source.version) or ():
            if line > change.line + change.gone:
                line += change.came - change.gone
            elif line > change.line:
                line = change.line
        return min(line, self.doc.lines - 1)

    def names(self, line, prefix=''):
        """Completion candidates on `line` as (name, kind), from the source's
        bindings (`Source.names`). They are as of its last reading; text
        typed since is read once typing rests."""
        if self.source.version != self.doc.version:
            self.ask(rest=True)
        text = self.doc.line(line)
        return self.source.names(self.back(line), len(text) - len(text.lstrip()), prefix)

    def land(self):
        """Take what the source finished reading. Returns True when anything arrived."""
        source, doc = self.source, self.doc
        # First the edits others made: what lands is brought forward to the text as it is.
        self.sync()
        if source.stale:
            # What its readings depend on changed (the live module's names): read again.
            source.stale = False
            self.ask()
        # Whichever session runs first lands the reading; each takes its own part once.
        source.land()
        if source.stamp == self.took:
            return False
        changes = doc.since(source.version)
        if changes is None:
            # Too many edits since to bring the facts forward: read again.
            self.ask()
            return False
        self.took = source.stamp
        if self.syntax.language == 'python':
            # The rows take the fold ranges. The pane runs again (rows may have moved, the
            # scroll range changed); the blocks repaint only when the folds on their lines differ.
            first, last = (self.shown[0][1], self.shown[-1][1] + self.shown[-1][0].count) if self.shown else (0, 0)
            if self.rows.land(source.native, [(change.line, change.column, change.gone, change.came) for change in changes],
                              first, last):
                self.touch()
            self.tick += 1
            self.settle()
        hits = source.native.hits(self.term) if self.term else None
        if hits is not None:
            self.put('hits', marks.Hits(hits, source.version))
        self.dots()
        return True

    # --- breakpoints -----------------------------------------------------
    def watch(self, breakpoints):
        """Show `breakpoints` ({source site key: state}, None for none). A
        breakpoint is kept by its statement's key, never by a line: the
        source finds the lines (`Source.placed`) and the dots land with its
        readings, then follow edits like every mark."""
        self.breakpoints = breakpoints
        keys = None if breakpoints is None else frozenset(breakpoints)
        if self.changed('breaks', (keys,)):
            self.keys = None if keys is None else tuple(keys)
            if keys is None:
                self.put('breaks', None)
            elif keys != self.dotted:
                # Not this session's own click (`stop` drew that one at once).
                self.read()

    def dots(self):
        """The landed reading's breakpoint lines as the 'breaks' marks. The
        source places the keys of every session on it; this one shows its
        own. A reading asked for before the last click lacks a key: it is
        not shown, the one asked for since is."""
        points, placed = self.breakpoints, dict(self.source.placed)
        if points is None or not set(points) <= set(placed):
            return
        self.dotted = frozenset(points)
        lines = [placed[key] for key in points if placed[key] is not None and points[key].get('enabled', True)]
        self.put('breaks', marks.breaks(lines, self.source.version))

    def stop(self, line):
        """Toggle a breakpoint on buffer `line`: on the first source site
        that covers it; a line that has some loses them. Lines without code
        take none."""
        points, source, was = self.breakpoints, self.source, self.back(line)
        found = source.site(was) if points is not None and source.spans(was, was) else ()
        if not found:
            return
        had = [(first, key) for first, key in found if key in points]
        for _, key in had:
            del points[key]
        if not had:
            points[found[0][1]] = {'enabled': True}
        # Shown at once on the lines the dots stand on now; the next reading confirms them.
        shown = {at for at, _ in self.marks['breaks'].on(0, self.doc.lines)} if self.marks.get('breaks') else set()
        shown ^= {self.forth(first) for first, _ in (had or found[:1])}
        self.put('breaks', marks.breaks(shown))
        self.dotted = frozenset(points)
        self.keys = tuple(points)
        self.sources['breaks'] = (self.dotted,)
        self.read()

    # --- marks -----------------------------------------------------------
    @property
    def detail(self):
        """The line whose error detail is open, or None. It is a mark of its
        own, so it stays on its line through edits and goes with it."""
        found = self.marks.get('detail')
        return found.lines.line(0) if found else None

    @detail.setter
    def detail(self, line):
        was = self.detail
        if line != was:
            self.marks['detail'] = None if line is None else marks.Marks([(line, marks.Mark('open'))])
            # The chips of the two lines show which one is open.
            for item in (was, line):
                if item is not None and item < self.doc.lines:
                    self.touch_lines(item, item)

    def put(self, name, found):
        """Show `found` (a `Marks`; None clears) as the set called `name`.
        A set computed for an older version is brought forward first; one too
        old to follow is dropped and the set it would replace stays. The
        blocks repaint only when the new set shows something else on them:
        most results that arrive after an edit (errors, tints, spelling,
        breakpoints) say what is on screen already."""
        if found is not None:
            found = marks.current(found, self.doc)
            if found is None:
                return
        was = self.marks.get(name)
        if found is not None or was is not None:
            self.marks[name] = found
            if not self.same(was, found):
                self.touch()

    def same(self, was, found):
        """Whether two mark sets show the same on the declared blocks: on
        their lines, and on the first lines of the scopes around them (a
        'tint' there colours the scope). Blocks out of sight are painted
        when they are declared again, so they need no look."""
        if not self.shown:
            return False
        first, last = self.shown[0][1], self.shown[-1][1] + self.shown[-1][0].count - 1
        spans = [(first, last)] + [(head, head) for head, *_ in self.rows.guides(first, last) if head < first]
        try:
            return all((was.on(low, high) if was is not None else []) == (found.on(low, high) if found is not None else [])
                       for low, high in spans)
        except Exception:
            return False        # marks whose data does not compare (a live value): painted again

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
        """Highlight every occurrence of `term` (case blind). A small text
        is read at once; a large one on the source's thread once the term
        rests, as typing in the text is: a term typed letter by letter is
        read once. (The current hit and stepping: editor/find.py.)"""
        self.term = term or ''
        self.put('hits', None)
        if term:
            if self.doc.size <= INLINE:
                self.read()
            else:
                self.ask(rest=True)

    def ranges(self, found):
        """Set the caller's fold layer from `(head, last)` line ranges
        (`Rows.gaps`: a range starts collapsed unless the user opened it)."""
        self.rows.gaps([tuple(item) for item in found or ()])
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
            sys.modules['meltygui.editor.history'].rebase(self.source, low, high, len(new))
        self.sync()

    # --- undo ------------------------------------------------------------
    def record(self, low, old, new, before, after):
        """The document's undo hook: the edit goes on the framework's stack."""
        from meltygui.editor import history
        history.record(self, low, old, new, before, after)

    def replay(self, step, undo):
        """Undo or redo one of this session's steps. Called by the
        framework's undo stack, at any time between runs. The step is the
        text's: the source changes once and every session on it follows
        (their carets stay on their characters). This session, the one that
        made the edit, restores its caret and shows where the text changed;
        when it has closed, the text is restored all the same."""
        doc = self.doc
        low, high = doc.size, 0
        for at, old, new in (reversed(step.parts) if undo else step.parts):
            gone, came = (new, old) if undo else (old, new)
            doc.splice(at, at + len(gone), came)
            low, high = min(low, at), max(high, at + len(came))
        step.version = doc.version
        if self.closed:
            return
        self.sync()
        doc.select(*(step.before if undo else step.after))
        self.step, self.landing = None, (low, max(high, low), True)
        if self.wake is not None:
            self.wake()

    # --- folds -----------------------------------------------------------
    def show(self, index):
        """Unfold whatever hides the line of `index`."""
        line = self.doc.line_of(index)
        if self.rows.hidden(line) and self.rows.reveal(line):
            self.touch()

    def toggle(self, line):
        """Collapse or expand the fold headed by `line`."""
        if not self.rows.toggle(line):
            return False
        self.settle()
        self.touch()
        return True

    def fold_all(self, closed):
        self.rows.fold_all(closed)
        self.settle()
        self.touch()

    def snapshot(self):
        """(first row, rows in all, text, file numbers) of the declared
        blocks as they are shown: what `EditorState` keeps so that a later
        session can paint before its text is loaded."""
        doc, rows, numbers = self.doc, self.rows, self.numbers
        if not self.shown:
            return 0, rows.count, '', []
        if rows.runs:
            lines = [line for block, first, _, _ in self.shown for line in range(first, first + block.count)
                     if not rows.hidden(line)]
            text = '\n'.join([doc.line(line) for line in lines])
        else:
            lines = range(self.shown[0][1], self.shown[-1][1] + self.shown[-1][0].count)
            text = '\n'.join(doc.rows(lines[0], len(lines)))
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
        return self.syntax.insets(self.doc.line(line), self.syntax.state(line))

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

    def widget(self, line, x, aside=True):
        """(entry, column, length) of the inline widget under content x on
        `line`, or None. A widget that is its token is not there while the
        caret is inside the token (`aside=False`: it is asked for all the same)."""
        doc, cw = self.doc, self.cw
        text = doc.line(line)
        inset = self.insets(line)
        for start, piece, kind in self.syntax.runs(text, self.syntax.state(line)):
            entry = inline.for_token(kind)
            if entry is None:
                continue
            left = self.gutter + (start + shift(inset, start) if inset else start) * cw
            if entry.lead:
                low, high = left - entry.lead * cw, left
            elif entry.trail:
                low, high = left + len(piece) * cw, left + (len(piece) + entry.trail) * cw
            elif aside and self.inside == (line, start):
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
            for start, piece, kind in self.syntax.runs(doc.line(line), self.syntax.state(line)):
                entry = inline.for_token(kind)
                if start <= column <= start + len(piece) and entry is not None and not (entry.lead or entry.trail or entry.solid):
                    found = (line, start)
        if found != self.inside:
            for item in (found, self.inside):
                if item is not None and item[0] < doc.lines:
                    self.touch_lines(item[0], item[0])
            self.inside = found
