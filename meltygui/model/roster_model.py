"""The project roster: every file's symbols and where they are used, as plain values.

    roster = project(path)                    # the roster of the project `path` is in: Melty's runtime keeps one per project
    roster.join(source)                       # an open Source: every reading it lands is its file's table
    rows, needs = roster.tints(source)        # what the buffer washes for the tints other files state
    roster.lookup(source, line, column)       # use -> definition, definition -> uses: (path, line, column, characters, guess)
    roster.watch(consumer, needs)             # `consumer.wake()` once what `needs` hangs on changed

The tables and the names across them live in Rust (native/source/src/roster.rs:
read its module text first) and are made on the roster's own thread, without
the interpreter lock. Python only says what Rust cannot know: which files are
the project's and where its imports are looked up (`gather`, once, on a pool
thread), and what a file's unsaved text is. That arrives as an event, never
by asking: `PendingSave` announces a queued edit (`listen`), `changed` notes
the path on the announcing thread, and a pool thread pushes the text
(`feed`). An open `Source` needs neither: `join` makes its readings its
file's table. A text pushed for a file a source is open on waits behind it.

Answers (`tints`, `lookup`, `resolve`, `uses`) may read files and take time
proportional to what they return: ask them off the render thread (the editor's
`Analysis` worker does). Each `tints` answer comes with what it hung on
(`needs`); when tables change, the roster's thread calls `woke`, which wakes
the watchers whose needs went stale and nobody else. Lines count from 0, as
a `Source`'s. To offer another answer: a method of `Ask` in roster.rs, a
`#[pymethods]` entry of `Roster` there, and a method here.
"""
import itertools
import os
import threading
import weakref

from meltygui.model._source_native import Roster as Native


def weakly(method):
    """A call of a bound method that does not keep its object alive (the native roster holds it)."""
    found = weakref.WeakMethod(method)

    def call():
        method = found()
        if method is not None:
            method()
    return call


class Roster:
    def __init__(self, root, context=None, paths=None, roots=()):
        """`context`: the project's `SourceContext` (source_context.py): what it owns, where its imports are
        looked up. Without one give `paths` and `roots` yourself (tests), or call `files` later."""
        self.root, self.context = os.path.realpath(root), context
        self.native = Native(weakly(self.woke))
        self.watchers = weakref.WeakKeyDictionary()    # consumer -> the needs of the answer it shows
        self.joined = weakref.WeakValueDictionary()    # path -> the open `Source` that is its file's table (`join`)
        self.dirty = {}                # path -> the count of the event that last touched it: what `feed` pushes
        self.count = itertools.count(1)
        self.feeding = False
        self.lock = threading.Lock()
        if paths is not None:
            self.files(paths, roots)

    def files(self, paths, roots=()):
        """The project's files (resolved paths) and the folders its imports are looked up in, in order.
        Files not read yet are read from disk on the roster's thread; files no longer listed leave."""
        self.native.files(list(paths), list(roots))

    def gather(self):
        """Tell the roster the files the project owns and its import folders. Walks the tree: a pool thread's job."""
        from meltygui.text_index import _walk_rel_files
        context = self.context
        paths = [os.path.realpath(os.path.join(self.root, relative)) for relative in _walk_rel_files(self.root) if relative.endswith('.py')]
        if context is not None:
            paths = [path for path in paths if context.owns(path, canonical_file=True)]
        folders = context.import_paths if context is not None else (self.root,)
        self.files(paths, dict.fromkeys(os.path.realpath(folder) for folder in folders if folder and os.path.isdir(folder)))

    def listen(self):
        """Follow the unsaved edits of the project's files, and gather its files (what `project` does once)."""
        from meltygui.core.runtime.background import Background
        from meltygui.editor.pending_save import PendingSave
        # (The same edge as the 'pending_save_changed' extension, which holds one callback: CodeDict's.)
        PendingSave.subscribe(self.changed)
        Background.submit_io(self.gather)

    def changed(self, address):
        """A queued edit of a file was set or dropped (on the queuing thread, which may be a frame's):
        note the path and leave. Also takes a plain path."""
        from meltygui.core.runtime.background import Background
        path = str(getattr(address, 'path', address))
        with self.lock:
            self.dirty[path] = next(self.count)
            if self.feeding:
                return
            self.feeding = True
        Background.submit_io(self.feed)

    def feed(self):
        """Push the text of every file noted as it is now, unsaved edits spliced in (a pool thread)."""
        from meltygui.editor.pending_save import PendingSave
        from meltygui.model.file_location_model import is_remote
        while True:
            with self.lock:
                if not self.dirty:
                    self.feeding = False
                    return
                path, generation = self.dirty.popitem()
            if is_remote(path) or not path.endswith('.py'):
                continue
            path = os.path.realpath(path)
            if self.owns(path):
                text = PendingSave.current_file_text(path, canonical_file=True)
                if text is not None:
                    self.push(path, text, generation)

    def owns(self, path):
        if self.context is not None:
            return self.context.owns(path, canonical_file=True)
        return path.startswith(self.root + os.sep)

    def push(self, path, text, generation):
        """A file's text as it is now. `generation` orders the pushes of one file: an older one is dropped."""
        self.native.push(path, text, generation)

    def join(self, source):
        """The readings `source` lands are its file's table from now on: what it shows is what other files see."""
        path = os.path.realpath(source.path)
        self.joined[path] = source
        source.native.join(self.native, path)

    def leave(self, source):
        """`source` is closed: its file is the disk's again, with its unsaved edits."""
        source.native.join()
        self.joined.pop(os.path.realpath(source.path), None)
        self.native.leave(os.path.realpath(source.path))
        self.changed(source.path)

    def tints(self, source):
        """(rows, needs): what the landed reading of `source` washes for tints other files state. A row is
        (line, column, characters, red, green, blue, scale): a `Source.native.tints()` row and the wash's
        strength (1 for a use of the tinted symbol itself). `needs`: for `watch` and `stale`."""
        return self.native.tints(source.native, os.path.realpath(source.path))

    def lookup(self, source, line, column):
        """What the name at `column` of `line` of the landed reading leads to in the project: (True, uses)
        on a definition's name, (False, [definition]) on a use, None elsewhere. Places are (path, line,
        column, characters, guess): a guess is a name nothing imports, or a member reached through a value."""
        return self.native.lookup(source.native, os.path.realpath(source.path), line, column)

    def resolve(self, path, chain, scope=None, guess=True):
        """(path, dotted name, line, column) of the entry the dotted `chain` names in file `path`, seen from
        the entry `scope` (a dotted name; None: the module), or None. By the tables alone: for a place in an
        open buffer ask `lookup`, which knows the buffer's locals."""
        return self.native.resolve(path, chain, scope, guess)

    def uses(self, path, name):
        """[(path, line, column, characters, guess)] of the uses of the entry `name` (dotted) of file `path`, or None."""
        return self.native.uses(path, name)

    def entries(self, path, first=0, last=2 ** 31):
        """The table rows of `path` whose definitions start on lines `first .. last` (`Source.table` entries)."""
        return self.native.entries(path, first, last)

    def named(self, name):
        """[(path, dotted name, kind, line, column, tint)] of the project's entries called `name`."""
        return self.native.named(name)

    def generation(self, path=None):
        """The generation at which what other files see of `path` last changed; without a path the roster's."""
        return self.native.generation(path)

    def stale(self, needs):
        return self.native.stale(needs)

    def watch(self, consumer, needs):
        """Call `consumer.wake()` (on the roster's thread) once something `needs` hangs on changed.
        The consumer asks again and watches its new needs; it is not kept alive."""
        with self.lock:
            self.watchers[consumer] = needs

    def woke(self):
        """Tables changed (on the roster's thread): wake who shows what hung on them."""
        with self.lock:
            watching = list(self.watchers.items())
        for consumer, needs in watching:
            if self.native.stale(needs):
                consumer.wake()

    def settle(self):
        """Wait until every table asked for is in (tests, measurements)."""
        self.native.settle()

    def close(self):
        self.native.close()


def project(path=None, context=None):
    """The roster of the project `path` is in: one per project root, kept by Melty's shared runtime
    (`Melty.rosters`) and handed to the models that use it (`Analysis(source, roster)`)."""
    from meltygui.code.source_context import analysis_project
    from meltygui.core.melty import Melty
    context = analysis_project(context, path)
    found = Melty.rosters.get(context.root)
    if found is None:
        found = Melty.rosters[context.root] = Roster(context.root, context)
        found.listen()
    return found
