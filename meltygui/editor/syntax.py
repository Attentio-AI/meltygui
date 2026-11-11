"""Python lexing for the editor: line string state, tokens, fold ranges.

The lexer is the pure code in `text_editor.py`. That module is slow to import
(it pulls libcst), so `warm` loads it on a thread and the editor paints plain
text until `ready()`. `tools()` is the blocking import for edit commands.

String state is kept per block (`Block.open`: the state each of its lines
starts in, `Block.exit`: the state after its last). The blocks up to
`Syntax.frontier` are settled; `Syntax.reach` settles more, a slice at a time,
so opening a string at the top of a long file never lexes the file in one frame.
"""
import threading
from bisect import bisect_right

from meltygui.editor import inline

_loaded = threading.Event()
_started = False
# Lever: cached token lists (one per distinct line text and string state).
TOKEN_CACHE = 20000
# Lever: blocks lexed in one frame while the string state catches up.
SLICE = 48
# Kinds whose adjacent tokens paint as one run (never the widget kinds).
JOINED = frozenset(('default', 'comment', 'string', 'string_doc', 'keyword', 'operator_word'))


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
        tools()
        # The undo manager too: the first edit then finds both loaded.
        import meltygui.editor.history  # noqa: F401
        if done is not None:
            done()
    threading.Thread(target=load, name='editor-syntax', daemon=True).start()


def structure(line):
    """What a line contributes to fold ranges: an edit that keeps it cannot
    change them, so no rescan is asked for."""
    body = line.lstrip(' ')
    return (len(line) - len(body), body[:1] == '#', body.split(' ', 1)[0].rstrip(':('), body.rstrip()[-1:] == ':')


def outline(text):
    """The fold ranges of a Python source as `Folds.set` arguments: scopes,
    blocks, comment runs, multi line strings and the import block. Reads the
    whole text: runs on the editor's worker."""
    from meltygui.editor.rows import nest
    ranges, defaults, keys = tools()._scope_fold_ranges(text)
    heads, spans, parents, ranges = nest([item for item in ranges if item[1] > item[0]])
    lines = text.split('\n')
    names, scopes = [], []
    for head, last in ranges:
        name = keys[(head, last)]
        names.append(name)
        line = lines[head]
        scopes.append((len(line) - len(line.lstrip(' ')), name[0] == 'scope') if name[0] in ('scope', 'block') else None)
    return heads, spans, parents, names, scopes, [keys[item] for item in defaults if item in keys]


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
    """Lexer state of one document, kept on its blocks."""
    __slots__ = ('cache', 'language', 'frontier', 'spilled', 'epoch')

    def __init__(self, language='python'):
        self.cache = {}
        self.language = language
        self.frontier = 0          # blocks before this one have settled string state
        self.spilled = False       # an edit changed the string state of the lines below it
        self.epoch = inline.EPOCH  # the widget registrations the cached insets were read from

    @property
    def live(self):
        return self.language == 'python' and _loaded.is_set()

    def edited(self, at):
        """Block `at` was rewritten: it and the blocks below settle again."""
        self.frontier = min(self.frontier, at)

    def reach(self, doc, upto):
        """Settle string state through block `upto`, at most a slice of it.
        Returns (blocks whose colours changed, whether `upto` was reached)."""
        if not self.live:
            return (), True
        blocks, changed = doc.blocks, []
        scan = tools()._iter_newline_states
        at, stop, budget = self.frontier, min(upto, len(blocks) - 1), SLICE
        while at <= stop and budget:
            block = blocks[at]
            enter = blocks[at - 1].exit if at else None
            before = block.open
            if not before or before[0] != enter:
                states = [enter]
                states.extend(state for _, state in scan(block.text, 0, enter))
                leave = states.pop() if len(states) > block.count else None
                if (states != before) if before else any(states):
                    changed.append(block)
                if before is not None and leave != block.exit:
                    self.spilled = True
                block.open, block.exit = states, leave
                budget -= 1
            at += 1
        self.frontier = at
        return changed, at > stop

    def state(self, doc, line):
        """The string state `line` starts in, as far as it is known."""
        at = doc.at_line(line)
        states = doc.blocks[at].open
        return states[line - doc.line0[at]] if states else None

    def tokens(self, text, state=None):
        """[(text, kind)] of one line that starts in string state `state`."""
        if not self.live:
            return [(text, 'default')] if text else []
        key = (text, state)
        found = self.cache.get(key)
        if found is None:
            if len(self.cache) > TOKEN_CACHE:
                self.cache.clear()
            found = self.cache[key] = tools()._tokenize_from(text, state) if text else []
        return found

    def runs(self, text, state=None):
        """[(column, text, kind)] of one line: tokens with neighbours of one
        kind joined, so a line paints in a few draw calls."""
        if not self.live:
            return [(0, text, 'default')] if text else []
        key = (text, state, 0)
        found = self.cache.get(key)
        if found is None:
            found, column = [], 0
            for piece, kind in self.tokens(text, state):
                if found and found[-1][2] == kind and kind in JOINED:
                    found[-1] = (found[-1][0], found[-1][1] + piece, kind)
                else:
                    found.append((column, piece, kind))
                column += len(piece)
            self.cache[key] = found
        return found

    def insets(self, text, state=None):
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

    def around(self, doc, index):
        """(text, local index, line starts, line states) of the block holding
        offset `index`: what the legacy string and comment helpers read."""
        at = doc.at_char(index)
        block = doc.blocks[at]
        return block.text, index - doc.char0[at], block.starts, block.open or [None] * block.count

    def quiet(self, doc, index):
        """Inside a string or comment: where suggestions stay closed."""
        if not self.live:
            return False
        return tools()._pos_in_string_or_comment(*self.around(doc, index))
