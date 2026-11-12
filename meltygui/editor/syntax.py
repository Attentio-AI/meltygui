"""Python colouring for the editor: the runs a line paints with.

The lexing is the session's `Source` (Rust): `Source.states` says which
string a line starts inside, `runs(line, state)` what the line's text is
made of. Nothing is kept per line here but a cache of the runs by line text.
The colours themselves and the edit helpers are still in `text_editor.py`,
which is slow to import (it pulls libcst): `warm` loads it on a thread and
the editor paints plain text until `ready()`. `tools()` is the blocking
import for edit commands.
"""
import threading
from bisect import bisect_right

from meltygui.editor import inline
from meltygui.model.source_model import runs

_loaded = threading.Event()
_started = False
# Lever: cached token lists (one per distinct line text and string state).
TOKEN_CACHE = 20000
# A string state (`Source.states`) as the legacy string and comment helpers know it.
LEGACY = (None, ('"""', 'string_doc'), ("'''", 'string_doc'), ('"""', 'string'), ("'''", 'string'),
          ('"""', 'fstring'), ("'''", 'fstring'))


def tools():
    """The legacy helper module; blocks on the first call until imported."""
    import meltygui.editor.text_editor as module
    _loaded.set()
    return module


def ready():
    return _loaded.is_set()


def warm(done=None):
    """Import the lexer off the calling thread; `done()` runs there when loaded."""
    global _started
    if _loaded.is_set() or _started:
        return
    _started = True

    def load():
        from meltygui.core.windowing.glfw_utils import park
        tools()
        # The undo manager too: the first edit then finds both loaded. And the table of what can be
        # imported (suggest.py, `shortcuts`): built here, not in the frame of the first word typed,
        # and not inside the frame that started this thread.
        import meltygui.editor.history  # noqa: F401
        park()
        from meltygui.code.code_checks import project_importables
        project_importables()
        if done is not None:
            done()
    threading.Thread(target=load, name='editor-syntax', daemon=True).start()


def structure(line):
    """What a line contributes to fold ranges and to the lines below it: an
    edit that keeps it cannot change them, so no reading is asked for."""
    body = line.lstrip(' ')
    return (len(line) - len(body), body[:1] == '#', body.split(' ', 1)[0].rstrip(':('), body.rstrip()[-1:] == ':',
            line.count('"""') + line.count("'''"))


def shift(inset, column):
    """Cells added before `column` on a line with `inset` (see `Syntax.insets`)."""
    at = bisect_right(inset[0], column)
    return inset[1][at - 1] if at else 0


def unshift(inset, cell):
    """The column whose caret is nearest visual cell `cell`: the inverse of
    `column + shift(inset, column)`. A point on added cells answers the
    column right after them."""
    before = 0
    for column, total in zip(*inset):
        if cell < column + before - .5:
            break
        if cell < column + total:
            return column
        before = total
    return max(int(cell - before + .5), 0)


class Syntax:
    """The colour runs of one session's lines, read from its `Source`."""
    __slots__ = ('cache', 'language', 'source', 'epoch')

    def __init__(self, language='python', source=None):
        self.cache = {}
        self.language = language
        self.source = source       # the session's `Source`: where string states come from
        self.epoch = inline.EPOCH  # the widget registrations the cached insets were read from

    @property
    def live(self):
        return self.language == 'python' and _loaded.is_set()

    def states(self, line, count=1):
        """The string state each of `count` lines from `line` starts in, as
        bytes: 0 in code; 255 while the text above was never read (it paints
        as code until the source's first reading lands). 16 is added for a
        line that starts inside a call's parentheses: `runs` colours a
        keyword argument at its start by it."""
        return self.source.native.states(line, count)

    def state(self, line):
        return self.source.native.states(line, 1)[0]

    def legacy(self, line):
        """The state of `line` as the legacy string and comment helpers take it."""
        state = self.state(line)
        return LEGACY[state & 15] if state != 255 and state & 15 < len(LEGACY) else None

    def runs(self, text, state=0):
        """[(column, text, kind)] of one line that starts in string state
        `state`: neighbours of one plain kind are one run, so a line paints
        in a few draw calls."""
        if not self.live:
            return [(0, text, 'default')] if text else []
        if state == 255:
            state = 0
        key = (text, state)
        found = self.cache.get(key)
        if found is None:
            if len(self.cache) > TOKEN_CACHE:
                self.cache.clear()
            found = self.cache[key] = [(column, text[column:column + size], kind) for column, size, kind in runs(text, state)]
        return found

    def insets(self, text, state=0):
        """The cells inline widgets add to one line, as (columns, totals):
        `totals[i]` cells sit before column `columns[i]` and everything after
        it. None for a line without any, which is nearly every line."""
        if not self.live:
            return None
        if self.epoch != inline.EPOCH:
            self.epoch = inline.EPOCH
            self.cache.clear()
        key = (text, state, 1)
        found = self.cache.get(key, self)
        if found is self:
            columns, totals, total = [], [], 0
            for column, piece, kind in self.runs(text, state):
                entry = inline.for_token(kind)
                if entry is not None and entry.lead:
                    total += entry.lead
                    columns.append(column)
                    totals.append(total)
                if entry is not None and entry.trail:
                    total += entry.trail
                    columns.append(column + len(piece))
                    totals.append(total)
            found = self.cache[key] = (columns, totals) if columns else None
        return found

    def quiet(self, line, column):
        """Inside a string or comment: where suggestions stay closed."""
        return self.live and self.source.native.inside(line, column)
