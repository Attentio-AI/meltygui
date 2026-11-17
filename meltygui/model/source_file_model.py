"""File: the text of one file on disk. The io layer under `Source(Toggles)`.

    file = opened(path)                 # one per file: everyone who names the file has the same text
    file.tree['GAIN'] = 0.75            # an edit by anyone is in the file on disk: nothing to save
    file.text = text                    # the whole text set: what differs is spliced
    draw_editor(file, ...)              # it is a `Source`: sessions, facts, the tree

The interface is model/source_scope_model.py (`Scope`, what `Source(Toggles)` returns); this module
and native/source/src/file.rs are what make "the code is the file" fast. A caller sees none of it:
there is no save, no dirty flag and no pending text.

- **Load.** `opened` reads nothing: the io thread (Rust, never holding the interpreter) reads the
  file and cuts it into chunks, a second one reads its facts with the tree. Code outside a frame that asks for
  the text waits for that (`ready`); a view never waits: it draws what is here and the frame loop
  takes the text when it arrives (`Bell`).
- **Write.** Every splice, by an editor, a tree or `text =`, follows to the file on the io thread:
  the whole text to a temp name, renamed over the file, and the compiled copies removed
  (`compiled`). Edits that pile up are one write.
- **Follow.** A change another program made arrives as one splice (`arrive`): every session and
  every tree on the text follows it like any edit. While the file holds a state the text has not
  taken, nothing is written: the file wins over an edit not yet written.

`FILES` is the owner of path to text. It is a module table and not `Melty.files`, as the rosters
are, because `Source(Toggles)` stands at the top of an app's module: importing `core.melty` there
costs 14 ms on the main thread before the first frame, which the app otherwise imports on its
import thread.

Levers: `framing` (who never waits), file.rs for everything about the disk (the temp name, fsync,
the encodings written, the events watched).
Not there: a file's text is kept for the process (no close); an edit lost to a file that changed
under it is not offered back (no conflict view); a file that is no UTF-8 is shown and never written.
Tests: tests/test_source_file.py.
"""
import os
import sys
import threading

from meltygui.model import _source_native
from meltygui.model.source_model import Source, Splice

FILES = {}        # real path -> File
_opening = threading.Lock()


def opened(path):
    """The text of the file at `path`: the same `File` for every caller, whatever name led to it
    (a link, a relative path). A file that is not there yet is an empty text; the first edit creates it."""
    path = os.path.realpath(path)
    found = FILES.get(path)
    if found is None:
        with _opening:
            found = FILES.get(path)
            if found is None:
                found = FILES[path] = File(path)
    return found


def compiled(path):
    """The files Python keeps the code of `path` compiled in: the io thread removes them after every
    write. A compiled copy is told from a newer source by its second and its size, and an edit
    (a digit dragged) keeps both."""
    if not path.endswith('.py'):
        return []
    from importlib.util import cache_from_source
    return [cache_from_source(path, optimization=level) for level in ('', 1, 2)]


def flush():
    """Wait until every edit made so far is in its file (tests, measurements: the exit does it itself)."""
    _source_native.flush()


def framing():
    """Whether a `@gui` frame runs on this thread: a frame never waits for a file."""
    host = sys.modules.get('meltygui.core.rendering.gui_prototype')
    return host is not None and host._window.get() is not None


class Bell:
    """What the frame loop calls when the io thread woke it for a file (`glfw_utils.drain_wakes`):
    the text takes what arrived before the next frame draws."""
    __slots__ = ('file', '__weakref__')

    def __init__(self, file):
        self.file = file

    def wake(self, node):
        self.file.land()


class File(Source):
    """A `Source` whose text is a file's. Made by `opened`, one per path."""

    def __init__(self, path, disk=True):
        Source.__init__(self, '', path)
        self.loaded = False            # the file's text is here
        self.error = None              # why the file is not written, as the io thread last said
        self.bell = None               # the `Bell` the io thread wakes, once a frame asked (`heed`)
        self.held = (-1, '')           # (head, the whole text): one str per version, for `text`
        # (`disk` off: a text that is no file, which its maker fills: model/source_version_model.py.)
        self.native.open(path if disk else None, compiled(path) if disk else [])

    def heed(self):
        """Have the io thread wake the frame loop when something arrives from the file. Asked
        by the first frame that reads the text: before one there is no loop, and callers wait."""
        if self.bell is None:
            from meltygui.core.windowing import glfw_utils
            self.bell = Bell(self)
            self.native.heed(*glfw_utils.waker(self.bell, 0))

    def ready(self, reading=False):
        """Have the file's text here, and with `reading` its first reading. Code outside a
        frame waits for the io thread, which is on it since `opened`; a frame takes what is there."""
        if framing():
            self.heed()
        elif reading or not self.loaded:
            self.native.wait(reading)
        self.arrive()

    def arrive(self):
        """Take what came from the file: its text once loaded, later a change another program
        made. Either is an edit like any other: logged, followed by every session, read into
        the tree. Costs one flag when nothing came. Returns whether the text changed."""
        found = self.native.arrive()
        if found is None:
            return False
        change, error = found
        if error is not None:
            self.error = error
            print(f'meltygui: Source {self.path}: {error}', file=sys.stderr)
        if change is None:
            return False
        grown, loading = self.grown, not self.loaded
        if loading:
            # The io thread reads the loaded text itself: nobody asks for it again, and the
            # views that read the tree are run when that reading lands (`Tree.feed`).
            self.loaded = self.inline = True
            if grown is not None:
                grown.asked = self.head
        elif grown is not None:
            grown.writing = True       # nothing to rest for: the change is read at once
        try:
            self.record(Splice(*change[:6]), *change[6:])
        finally:
            if grown is not None:
                grown.writing = False
        return True

    def land(self):
        self.arrive()
        return Source.land(self)

    def splice(self, low, high, new, version=None, by=None):
        if not self.loaded:
            self.ready()
        return Source.splice(self, low, high, new, version, by)

    @property
    def text(self):
        """The whole text as one str, kept per version: reading it every frame costs one copy an edit."""
        if self.loaded:
            self.arrive()
        else:
            self.ready()
        head = self.head
        if self.held[0] != head:
            self.held = (head, self.native.text())
        return self.held[1]

    @text.setter
    def text(self, text):
        if not self.loaded:
            self.ready()
        self.put(text)

    @property
    def tree(self):
        """The code as keys and values (`Source.tree`). The first reading is the io thread's; outside
        a frame it is waited for, so the tree is whole when this returns."""
        grown = self.grown
        if grown is None:
            from meltygui.model.tree_model import Tree
            self.ready(reading=True)
            grown = self.grown = Tree(self)
            self.models.add(grown)
            if self.head <= 1:
                grown.asked = 1        # the loaded text: read by the io thread (`arrive`)
            elif framing():
                grown.ask()            # edited since it was loaded
            else:
                grown.read()
        grown.sync()
        return grown.top
