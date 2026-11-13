"""Ctrl+B for an `Editor`: what the name at the caret leads to.

A use leads to its definition, a definition's name to its uses: one place is
jumped to, several open the usage picker. The facts are the session's
`Source` (`Source.lookup`: the symbols the text defines) or, given the
project's roster, `Roster.lookup` (across files); a function's local or
parameter leads from a use to its binding (`Source.bound`). All of it is
asked on a pool thread (`seek`), of the landed reading: a press on text
typed since waits for its reading first.

Ctrl+Shift+B opens the caret's line in the external editor (`external`).

`Lookup` is the session's state of it and the picker's `Menu`, painted by the
one `choices` view like every list. Places are plain rows (`Place`); a place
in another file is handed to the caller (`draw_editor(on_open=)`), the
editor opens no file. To list something else per row, give `Place` the
field, fill it in `seek` and show it in `Lookup.offer`.
"""
import logging
import os
import threading
import time
from collections import namedtuple

from meltygui.core.windowing.glfw_utils import park
from meltygui.editor import edit, marks
from meltygui.editor.suggest import Menu
from meltygui.editor.syntax import tools

# Lever: the places a picker lists; the rest are counted in its last row.
MOST = 200
# Lever: characters of a line's text a picker row shows.
WIDE = 72
# Lever: lines gone over between two looks for a frame (`park`).
PIECE = 2048
# The flash on a name that leads nowhere.
MISS = (.9, .28, .22)

# Where a name leads: `path` None is the buffer itself, lines and columns count from 0 in the reading
# the answer is of, `guess` as the roster says it, `number` the file line shown, `text` the line's.
Place = namedtuple('Place', 'path line column chars guess number text')
# What `seek` found: `places` (at most `MOST`), how many `more` there are, the source version they are of.
Answer = namedtuple('Answer', 'definition places more version')


def local(source, line, column):
    """(False, [(line, column, characters)]) of the binding the local name
    used at `column` of `line` resolves to, or None: on no use, on a member,
    on the binding itself."""
    for _, start, chain, _ in source.references(line, line):
        name = chain.split('.')[0]
        if start <= column <= start + len(name) and name.isidentifier():
            text = source.native.span(line, line)
            found = source.bound(name, line, len(text) - len(text.lstrip()))
            if found is None or found[1] == line:
                return None
            at = next((row[1] for row in source.bindings(found[1], found[1]) if row[2] == name), None)
            return None if at is None else (False, [(found[1], at, len(name))])
    return None


def pick(text, wanted):
    """{line: its text} of the sorted lines `wanted` of `text`, found break by
    break: no split of the whole text, and a frame gets its turn (`park`)."""
    found, at, line = {}, 0, 0
    for want in wanted:
        while line < want and at >= 0:
            at = text.find('\n', at) + 1 or -1
            line += 1
            if not line % PIECE:
                park()
        if at < 0:
            break
        end = text.find('\n', at)
        found[want] = text[at:end if end >= 0 else len(text)]
    return found


def texts(rows, source, roster):
    """The text of each row's line ((path or None, line, ..) rows): the
    buffer's from its landed reading, another open source's from its own
    (`Roster.joined`), a file's as the roster read it (unsaved edits in)."""
    files, found = {}, []
    for row in rows:
        if row[0] is not None:
            files.setdefault(row[0], set()).add(row[1])
    for path, lines in files.items():
        other = roster.joined.get(path) if roster is not None else None
        if other is not None:
            files[path] = {line: other.native.span(line, line) for line in lines}
        else:
            from meltygui.editor.pending_save import PendingSave
            files[path] = pick(PendingSave.current_file_text(path, canonical_file=True) or '', sorted(lines))
        park()
    for path, line, *_ in rows:
        found.append((source.native.span(line, line) if path is None else files[path].get(line, '')).strip()[:WIDE])
    return found


def seek(source, roster, line, column, numbers=None):
    """What the name at `column` of `line` of the landed reading of `source`
    leads to, as an `Answer`, or None on no name. `roster`: the project's
    (None: the text's own symbols only). `numbers`: the buffer's file lines
    (`rows.Numbers`). Reads files and whole tables: never on the render thread."""
    own = os.path.realpath(source.path) if source.path else None
    while True:
        version = source.version
        found = roster.lookup(source, line, column) if roster is not None and own else source.lookup(line, column)
        if found is None:
            found = local(source, line, column)
        if found is None:
            return None
        definition, rows = found
        # (path or None for the buffer, line, column, characters, guess): the buffer's own places first.
        rows = [(None if row[0] == own else row[0], *row[1:]) if len(row) == 5 else (None, *row, False) for row in rows]
        rows.sort(key=lambda row: row[0] is not None)
        listed = rows[:MOST]
        shown = texts(listed, source, roster)
        if source.version == version:
            break       # (else another reading landed meanwhile: its lines are others)
    number = lambda path, at: path is None and numbers is not None and numbers.number(at) or at + 1
    places = [Place(path, at, start, chars, guess, number(path, at), text) for (path, at, start, chars, guess), text in zip(listed, shown)]
    return Answer(definition, places, len(rows) - len(listed), version)


def external(path, line):
    """Open file line `line` of `path` in the external editor (IntelliJ), as
    Ctrl+Shift+B does: on a thread of its own, the launcher waits for the
    running instance to answer. Lever: another editor is another launcher here."""
    from meltygui.utils.jump_to_code import open_in_intellij
    threading.Thread(target=open_in_intellij, args=(str(path), line), daemon=True, name='open_in_intellij').start()


def ahead(doc, version, line):
    """Where line `line` of the text at `version` is now (`Editor.forth`, from any version)."""
    for change in doc.since(version) or ():
        if line > change.line + change.gone:
            line += change.came - change.gone
        elif line > change.line:
            line = change.line
    return min(line, doc.lines - 1)


class Lookup(Menu):
    """A session's Ctrl+B: the lookup under way and the usage picker
    (`rows` are (line text, 'file:line'), `places` the place of each)."""

    def __init__(self):
        super().__init__()
        self.places, self.version = [], 0
        self.pending = None        # the lookup running on the pool (a future)
        self.owed = False          # asked while the text was ahead of its reading: sent once that landed
        self.at = 0                # the caret the press was made at: an answer for another place is dropped

    def close(self):
        self.rows, self.places, self.first, self.index, self.picked, self.cells = [], [], 0, 0, None, None

    def drop(self):
        """Forget the press too: whatever it finds is not shown."""
        self.close()
        self.pending, self.owed = None, False

    def extra(self, at):
        return None, 'guess' if at < len(self.places) and self.places[at].guess else ''

    def ask(self, ed, roster):
        """Ctrl+B at the caret. The answer is taken by `arrive` on a later run."""
        self.drop()
        self.at, self.owed = ed.doc.caret, True
        if ed.source.version != ed.doc.version:
            ed.read()       # at once for a small text, else on the source's thread: its landing wakes the view
        self.send(ed, roster)

    def send(self, ed, roster):
        from meltygui.core.runtime.background import Background
        if ed.source.version != ed.doc.version:
            return
        self.owed = False
        line, column = ed.doc.place(self.at)
        self.pending = Background.submit_io(seek, ed.source, roster, line, column, ed.numbers)
        self.pending.add_done_callback(lambda done, wake=ed.wake: wake and wake())

    def arrive(self, ed, roster, on_open):
        """Take the answer once it is in: jump to its one place, open the
        picker for several, flash the name for none. Returns True when the
        caret jumped."""
        if self.owed:
            self.send(ed, roster)
        future = self.pending
        if future is None or not future.done():
            return False
        self.pending = None
        if ed.doc.caret != self.at:
            return False        # the caret went elsewhere meanwhile
        if future.exception() is not None:
            logging.getLogger(__name__).error('lookup failed', exc_info=future.exception())
        found = future.exception() is None and future.result()
        if not found or not found.places:
            return self.miss(ed)
        self.version = found.version
        if len(found.places) == 1 and not found.more:
            return self.go(ed, found.places[0], on_open)
        self.offer(ed, found)
        return False

    def offer(self, ed, found):
        """Open the picker on an answer's places."""
        name = os.path.basename(ed.source.path) if ed.source.path else ''
        self.places = found.places
        rows = [(place.text, f'{os.path.basename(place.path) if place.path else name}:{place.number}'.lstrip(':')) for place in found.places]
        if found.more:
            rows.append((f'and {found.more} more', ''))
        self.index = self.first = 0
        self.set(rows)
        # (Measured once here: the list may be long.)
        self.cells = max(len(text) + len(tag) + (7 if place.guess else 0) for (text, tag), place in zip(rows, found.places)) + 4

    def take(self, ed, on_open):
        """The row picked by Enter or a click. Returns True when the caret jumped."""
        at, self.picked = self.picked, None
        if at is None or at >= len(self.places):
            return False        # the count row leads nowhere
        place = self.places[at]
        self.close()
        return self.go(ed, place, on_open)

    def go(self, ed, place, on_open):
        """Jump to a place: the caret onto a name of the buffer (the view
        reveals and flashes `ed.landing`), another file's to the caller."""
        doc = ed.doc
        if place.path is not None:
            if on_open is None:
                return self.miss(ed)
            on_open(place.path, place.number, place.column)
            return False
        low = doc.index(ahead(doc, self.version, place.line), place.column)
        doc.select(low)
        ed.landing = (low, low + place.chars, True)
        return True

    def outside(self, ed, path=None):
        """Ctrl+Shift+B: the caret's file line in the external editor
        (`external`). A buffer without a file has nowhere to go: the word flashes."""
        path = path or ed.source.path
        if not path:
            return self.miss(ed)
        line = ed.doc.line_of(ed.doc.caret)
        external(path, ed.numbers.number(line) or line + 1)

    def miss(self, ed):
        """Nothing to jump to: the word at the caret flashes red."""
        from meltygui.editor.editor import FLASH
        doc = ed.doc
        low, high = edit._near(doc, tools()._select_unit_left, doc.caret), edit._near(doc, tools()._select_unit_right, doc.caret)
        (line, column), stop = doc.place(low), doc.place(high)
        ed.put('flash', marks.Marks([(line, marks.Mark('flash', column, max(stop[1], column + 1) if stop[0] == line else None, MISS))]))
        ed.flash = time.monotonic() + FLASH
        if ed.wake is not None:
            threading.Timer(FLASH, ed.wake).start()
        return False
