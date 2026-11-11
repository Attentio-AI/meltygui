"""Python lexing for the editor: line string state, tokens, fold ranges.

The lexer is the pure code in `text_editor.py`. That module is slow to import
(it pulls libcst), so `warm` loads it on a thread and the editor paints plain
text until `ready()`. `tools()` is the blocking import for edit commands.
"""
import threading

_loaded = threading.Event()
_started = False
# Lever: cached token lists (one per distinct line text and string state).
TOKEN_CACHE = 20000
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
        if done is not None:
            done()
    threading.Thread(target=load, name='editor-syntax', daemon=True).start()


def structure(line):
    """What a line contributes to fold ranges: a change here means a rescan."""
    body = line.lstrip(' ')
    return (len(line) - len(body), body[:1] == '#', body.split(' ', 1)[0].rstrip(':('), body.rstrip()[-1:] == ':')


class Syntax:
    """Lexer state of one document, updated edit by edit."""
    __slots__ = ('text', 'starts', 'open', 'cache', 'language')

    def __init__(self, language='python'):
        self.text = self.starts = self.open = None
        self.cache = {}
        self.language = language

    @property
    def live(self):
        return self.language == 'python' and _loaded.is_set()

    def update(self, text, below=None):
        """Follow `text`. Returns the first line whose opening string state
        changed below the edit (so its colours did), or None. `below` is
        (old line, new line): where the untouched tail starts, when known."""
        if not self.live or text is self.text:
            return None
        before = self.open
        self.starts, self.open = tools()._update_line_open(self.text, self.starts, self.open, text)
        self.text = text
        after = self.open
        if before is None or before is after:
            return None
        if below is not None and before[below[0]:] == after[below[1]:]:
            return None
        # Compare from the end: the common suffix is what stayed the same.
        old, new = len(before), len(after)
        tail = 0
        while tail < old and tail < new and before[old - 1 - tail] == after[new - 1 - tail]:
            tail += 1
        head = 0
        limit = min(old, new) - tail
        while head < limit and before[head] == after[head]:
            head += 1
        return head if head < new - tail else None

    def tokens(self, line, text):
        """[(text, kind)] of one line, coloured for its opening string state."""
        if not self.live or self.open is None or line >= len(self.open):
            return [(text, 'default')] if text else []
        state = self.open[line]
        key = (text, state)
        found = self.cache.get(key)
        if found is None:
            if len(self.cache) > TOKEN_CACHE:
                self.cache.clear()
            found = self.cache[key] = tools()._tokenize_from(text, state) if text else []
        return found

    def runs(self, line, text):
        """[(column, text, kind)] of one line: tokens with neighbours of one
        kind joined, so a line paints in a few draw calls."""
        if not self.live or self.open is None or line >= len(self.open):
            return [(0, text, 'default')] if text else []
        key = (text, self.open[line], 0)
        found = self.cache.get(key)
        if found is None:
            found, column = [], 0
            for piece, kind in self.tokens(line, text):
                if found and found[-1][2] == kind and kind in JOINED:
                    found[-1] = (found[-1][0], found[-1][1] + piece, kind)
                else:
                    found.append((column, piece, kind))
                column += len(piece)
            self.cache[key] = found
        return found

    def quiet(self, index):
        """Inside a string or comment: where suggestions stay closed."""
        if not self.live or self.open is None:
            return False
        return tools()._pos_in_string_or_comment(self.text, index, self.starts, self.open)

    def folds(self, text):
        """(ranges, keys, defaults) for `Folds.set`: scopes, blocks, comment
        runs, multi line strings and the import block."""
        if not self.live:
            return [], {}, []
        ranges, defaults, keys = tools()._scope_fold_ranges(text)
        ranges = [item for item in ranges if item[1] > item[0]]
        return ranges, {item: keys[item] for item in ranges}, [item for item in defaults if item in keys]
