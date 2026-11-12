"""Source: a text and the facts derived from it, as plain values.

    source = Source(text, path, version)
    source.splice(low, high, new)             # every edit, in characters: by an editor session or by anyone else
    source.ask(term, rows=rows)               # facts for the text as it is now
    if source.land():                         # on a later frame
        rows.land(source.native, changes, first, last)
        source.hits, source.defs

The text and the reading live in Rust (native/source; build it with
`tools/build_source.py`). An edit costs its own chunk of lines whatever the
file size; facts are read on a thread that never holds the interpreter lock,
not even to say they wait: it wakes each session's view through a handle
of the frame loop (`waker`: `RetainedCache.waker(node)`). Landing them costs
nothing per fact: they stay in Rust and cross where they are read. The fold
ranges go to the asking session's `Rows` (editor/rows.py), which keeps them
on their lines; `hits` is a `Hits` that hands out the lines asked for; `defs`
converts the whole table when first read. `version` is the caller's stamp of the text
the landed facts were read from: bring them forward over the edits made
since (`Document.since`). `stamp` rises whenever facts arrive.
The source owns the text, its version (`head`) and the log of the last
splices (`since`). Anyone may splice it. Every editor session on it
(`sessions`: editor/editor.py) follows the splices it did not make, is woken
for them and for every reading, and gets its own fold ranges and search hits
out of one reading. Breakpoint keys, lint settings and live names are the
source's: every session sees the same.

Facts: fold ranges, `defs` (is class, name, line, last line, indent, parent
index), the bindings of every scope, the names used and the calls made
(`names`, `bound`, `bindings`, `references`, `calls`, asked by line) and `hits` (line, column, end column) of `term`; `read` returns the
whole tables as plain values at once. To add one: collect it in `Facts::read`
(facts.rs), give the native `Source` a method that returns it (lib.rs) and
this class a `fact` property.
"""
import builtins
import sys
import weakref
from collections import namedtuple

try:
    from meltygui.model import _source_native
    from meltygui.model._source_native import Source as Native, Rows, runs, LINES   # noqa: F401  (`Rows`: see editor/rows.py)
except ImportError as error:
    raise ImportError('The Rust `Source` extension is not built. Installing the checkout builds it '
                      '(`uv pip install -e .`); `python tools/build_source.py` builds it in place.') from error

# What every module sees without binding it: never an undefined name.
_source_native.known(dir(builtins) + ['__file__', '__name__', '__doc__', '__package__', '__spec__', '__loader__', '__builtins__',
                                      '__debug__', '__path__', '__class__', '__annotations__', '__dict__', '__module__', '__qualname__'])
# The names `runs` colours as builtins are this interpreter's.
_source_native.builtins([name for name in dir(builtins) if not name.startswith('_') and name not in ('True', 'False', 'None')])


# What one edit did, in lines and columns: the text from (line, column) over
# `gone` line breaks to column `old` became text over `came` line breaks
# ending at column `new`. Everything that follows edits reads only this.
Splice = namedtuple('Splice', 'line column gone old came new')
# Lever: splices kept for sessions and results that are behind the text.
LOG = 512


class Source:
    def __init__(self, text='', path=None, version=0):
        self.path = path
        self.native = Native(text, version)
        self.wake = None           # called when what readings depend on changed (`live`, `signatures`), besides every session's
        self.waker = None          # (handle, token) the reading thread wakes when facts wait, besides every session's (`ask`)
        # The editor sessions on this text. Each has `term`, `rows`, `keys`, `wake` and `waker`; the source holds none of them alive.
        self.sessions = weakref.WeakSet()
        # The last splices as (version after, Splice, offset, characters gone, characters come, who made it), from version `base` on.
        self.log, self.base = [], version
        self.version = -1          # of the landed facts
        self.term = ''             # what `hits` are the occurrences of
        self.stamp = 0             # rises with every arrival
        self.lint = False          # readings also look for undefined names and misfitting calls (`problems`)
        self.stale = False         # what the readings depend on changed (`live`): the writer asks again
        self.keys = None           # source site keys whose lines readings find (`placed`, `site`), besides every session's
        self.found = {}            # the landed fact tables read so far, by name

    @property
    def text(self):
        """The whole text as one string: a copy, for where all of it is needed."""
        return self.native.text()

    # The version the text is at: it rises with every splice.
    head = property(lambda self: self.native.version)

    def splice(self, low, high, new, version=None, by=None):
        """Put `new` in place of characters `low .. high`: the one way the
        text changes, for anyone. `by`: the session that makes the edit
        (code that is no editor passes none). Every other session on the
        source is woken and follows (`since`): rows, marks, caret. An edit
        from outside is no undo step, so the steps kept for this text are
        moved over it (editor/history.py). Returns the `Splice`."""
        gone = min(high, self.native.size) - low
        change = Splice(*self.native.splice(low, high, new, version))
        self.log.append((self.native.version, change, low, gone, len(new), by))
        if len(self.log) > LOG * 2:
            self.base = self.log[LOG - 1][0]
            del self.log[:LOG]
        if by is None and 'meltygui.editor.history' in sys.modules:
            sys.modules['meltygui.editor.history'].rebase(self, low, low + gone, len(new))
        for session in self.each():
            if session is not by and session.wake is not None:
                session.wake()
        return change

    def since(self, version):
        """The log entries of the splices made after `version`, oldest
        first, or None when they are no longer kept."""
        if version < self.base:
            return None
        log, at = self.log, len(self.log)
        while at and log[at - 1][0] > version:
            at -= 1
        return log[at:]

    def each(self):
        """The sessions on the source (a copy: another thread may ask)."""
        while True:
            try:
                return list(self.sessions)
            except RuntimeError:
                pass

    def ask(self, term='', rest=0., now=False, rows=None):
        """Read the facts of the text as it is now, and the occurrences of
        `term`. `rest`: seconds to wait for a newer request first (typing).
        `now`: read here instead of on the thread (small texts). `rows`: the
        asking session's `Rows`. One reading serves every session on the
        source: each finds its own fold ranges and its own term's hits in
        it, and the keys of all of them are placed."""
        from meltygui.core.runtime.toggles import Toggles
        sessions = self.each()
        keys = [self.keys] + [session.keys for session in sessions]
        keys = None if all(found is None for found in keys) else list({key for found in keys for key in found or ()})
        others = [(session.term, session.rows) for session in sessions if session.rows is not rows]
        # The reading thread holds no interpreter: it wakes by handle (`RetainedCache.waker`), never by calling Python.
        wakers = [found for found in [self.waker] + [session.waker for session in sessions] if found is not None]
        self.native.ask(wakers, term, Toggles.TextEditor.block_fold_ranges, rest, rows, now, self.lint, keys, others)
        if now:
            # Read already: the sessions that did not ask find it on their next run.
            for session in sessions:
                if session.rows is not rows and session.wake is not None:
                    session.wake()

    def woke(self):
        """What the readings depend on changed (`live`, `signatures`: from
        any Python thread): every session on the source is told, and
        whoever holds `wake`. (A reading that waits is told through the
        `waker`s instead: its thread never runs Python.)"""
        for wake in [self.wake] + [session.wake for session in self.each()]:
            if wake is not None:
                wake()

    def land(self):
        """Take what was read. Returns True when facts arrived."""
        found = self.native.land()
        if found is None:
            return False
        self.version, self.term = found
        self.found = {}
        self.stamp += 1
        return True

    def fact(self, name):
        """A landed fact table, converted when it is first read."""
        if name not in self.found:
            self.found[name] = getattr(self.native, name)()
        return self.found[name]

    defs = property(lambda self: self.fact('defs'))
    hits = property(lambda self: self.fact('hits'))

    # Facts asked by line, as of the landed reading (`version`). Lines are the ones of that text.
    def names(self, line, indent=0, prefix=''):
        """Completion candidates on `line` (indented `indent`) as (name, kind):
        the names bound in the scopes around it, innermost first ('param',
        'local', 'var', 'function', 'class', 'module'), then other names
        written near it ('name'). Only names holding `prefix`, case blind."""
        return self.native.names(line, indent, prefix)

    def bound(self, name, line, indent=0):
        """(kind, line) of the binding `name` resolves to from `line`, or None."""
        return self.native.bound(name, line, indent)

    def references(self, first, last):
        """The names used on lines `first .. last`: (line, column, dotted
        chain, the statement binds it). Not what a `def`, `class`, parameter,
        import, `except`, `global` or keyword argument only names."""
        return self.native.references(first, last)

    def calls(self, first, last):
        """The calls of dotted chains on lines `first .. last`: (line, column,
        chain, positional arguments or -1 when the call closes out of reach,
        keyword names, has a `*` argument, has a `**` argument)."""
        return self.native.calls(first, last)

    def statements(self, first, last):
        """The statements that start on lines `first .. last`: (first line,
        last line). A compound statement is its header."""
        return self.native.statements(first, last)

    def parameters(self, line):
        """The parameters of the function whose `def` is on `line`: (name,
        line, column, where its default value is as (line, column, last
        line, end column) or None)."""
        return self.native.parameters(line)

    def bindings(self, first, last):
        """The bindings on lines `first .. last`: (line, column, name, kind,
        index in `defs` of the scope it is bound in or -1 for the module).
        Kinds: def, class, param, assign, import, for, with, except, comp,
        global, nonlocal."""
        return self.native.bindings(first, last)

    def live(self, names):
        """Tell the source the names its running module has (plain data: the
        keys of the module's namespace). A name among them is not undefined.
        Push them whenever they may have changed (an import, a hotswap),
        from wherever that is noticed (any thread): never asked for by a
        frame. The source's writer is woken to have the text read again."""
        self.native.live(list(names))
        self.stale = True
        self.woke()

    def signatures(self, specs):
        """Tell the source the signatures of what its calls reach in the
        running process: {chain: rows, or None when it is not to be judged}
        for the chains of `callees` (plain data; `editor_model.signature`
        makes the rows). Pushed like `live`, from any thread: never asked
        for by a frame."""
        self.native.signatures(specs)
        self.stale = True
        self.woke()

    def spans(self, first, last):
        """Where the code of lines `first .. last` stands: (line, column it
        starts at, column it ends at, (start, end) of the right-hand side
        when the line assigns outside brackets, else None). What the
        live-value overlays draw over (`live_view_views._code_end_col`,
        `_rhs_span`); lines without code are left out."""
        return self.native.spans(first, last)

    def label(self, line, label):
        """(column, end column) of the name `label` on `line` (of a dotted
        label: its last part), or None: what a live value's box goes around
        (`live_view_views._label_box_span`)."""
        return self.native.label(line, label)

    def closes(self, first, last):
        """(line, column, last line, end column) of the calls that start on
        lines `first .. last`: the span of a `live_view(..)` call token."""
        return self.native.closes(first, last)

    # The statement table: (line, column, last line, end column, kind, key path) of every statement and
    # compound header, the source sites of `melty_scan.SourceSiteIndex` (what a breakpoint is kept by).
    # The whole text is gone over when it is first read: ask for it off the render thread.
    sites = property(lambda self: self.fact('sites'))

    def site(self, line):
        """[(first line, key path)] of the source sites that cover `line`, in
        source order: what a breakpoint on that line is kept by, never the
        line. Empty until a reading asked for with `keys` set has landed."""
        return [(first, tuple(key)) for first, key in self.native.site(line)]

    # [(key path, first line or None)] of `keys` in the landed reading: where each breakpoint stands.
    placed = property(lambda self: [(tuple(key), line) for key, line in self.fact('placed')])

    def lookup(self, line, column):
        """What the name at `column` of `line` leads to, among the symbols
        this text defines: (True, uses) on a definition's name, (False,
        [definition]) on a use; places are (line, column, characters). None
        elsewhere. Lines are the landed reading's (`Editor.back` / `forth`)."""
        return self.native.lookup(line, column)

    # The symbol table as plain rows (entries, imports, star imports): `native/source/src/facts.rs`, `table`.
    table = property(lambda self: self.fact('table'))
    undefined = property(lambda self: self.fact('undefined'))
    # What cannot be Python, as far as the reader is sure: (line, CPython's message) for brackets and an open triple-quoted string.
    flaws = property(lambda self: self.fact('flaws'))
    # The chains called whose signatures `signatures` has not given yet.
    callees = property(lambda self: self.fact('callees'))
    miscalls = property(lambda self: self.fact('miscalls'))

    @property
    def misfits(self):
        """[(file line, message)] of the landed reading's calls that do not
        fit what they call (`miscalls`; facts.rs and call.rs, which err on
        silence): a definition in this text, or what `signatures` told of
        the running process. A literal of the wrong type is told only while
        `Toggles.TextEditor.lint_literal_types`."""
        from meltygui.core.runtime.toggles import Toggles
        typed = Toggles.TextEditor.lint_literal_types
        return [(line + 1, message) for line, _, message in self.miscalls if typed or '() expected ' not in message]

    @property
    def problems(self):
        """[(file line, message)] of the landed reading's sure syntax errors
        (`flaws`), undefined names and misfitting calls (`lint` must be on
        for the last two): what `draw_editor` takes as
        `errors`. Every rule errs on silence (facts.rs, `undefined`): a name
        the running module has, or one that only stands in an annotation, is
        no error."""
        if 'problems' not in self.found:
            names = [(line + 1, f"name '{name}' is not defined") for line, _, name, live, noted in self.undefined if not live and not noted]
            flaws = [(line + 1, message) for line, message in self.flaws]
            self.found['problems'] = sorted(flaws + names + self.misfits, key=lambda found: found[0])
        return self.found['problems']

    def read(self, term=''):
        """(version, folds, defs, hits) of the text as it is now, as plain
        values, read on the calling thread: for callers outside a frame."""
        from meltygui.core.runtime.toggles import Toggles
        return self.native.read(term, Toggles.TextEditor.block_fold_ranges)

    def settle(self):
        """Have everything asked for read now, and wait for it (tests)."""
        self.native.settle()

    def close(self):
        """End the reading thread. The next `ask` starts one again."""
        self.native.close()
