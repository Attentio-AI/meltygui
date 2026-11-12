"""Spell check of an editor's text, as a decoration.

    spelling = Spelling(source)
    draw_editor(source, state=state, marks={'spelling': spelling.marks})

The model follows its source by events (`Source.models`: every splice calls
`feed`) and wakes the views that read `marks` when a result lands
(`Readers`): a caller makes no call per frame and may be a cached view.
`feed()` is still the caller's after it switched the setting.

While `Toggles.TextEditor.enable_spell_check` is on, the words
`editor/spell_check.py` does not know get a wavy underline. A worker reads
the text once it has rested (`QUIET`) and swaps in a new `Marks` set tagged
with the version it read, so the editor keeps it on its words through what
was typed meanwhile and until the next result. Lines are checked one by one
and remembered by their text: after an edit only the lines that changed are
looked up again. The dictionary loads on the worker, the first time the
setting is on. Nothing here runs on the render thread, and the worker shares
the interpreter with it: the text is read `SLICE` lines at a time and the
worker gives way to a frame between them (`glfw_utils.park`).

What is checked is every word, read the way code spells it
(`editor/spell_check.py`: delimiters, camel case parts, code's own terms).
To check only comments and strings, filter in `Spelling.spans` (the source
knows where they are: `Source.native.inside`).
"""
import threading

from meltygui.core.rendering.gui_readers import Readers
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import park

# Seconds the text must rest before it is checked.
QUIET = 0.4
# Lever: lines read and checked in one go before the worker looks for a frame to give way to: what a
# frame that starts meanwhile waits for (about half a millisecond when no line is remembered).
SLICE = 100
# Lever: remembered line texts; beyond it the memory starts over.
KEPT = 50000
# The underline (`SHAPES` in view/editor_view.py) and its colour: teal, so it never reads as an error.
SHAPE, COLOR = 'squiggle', (.16, .71, .65)


class Spelling:
    def __init__(self, source):
        self.source = source
        self.readers = Readers()   # the views that read `marks`: run again when a result lands
        self._marks = None         # `marks`
        self.checked = -1          # the source version `marks` belongs to
        self.fed = -1              # the version the worker was last woken for
        self.known = {}            # line text -> the marks of its unknown words
        self._wake = threading.Event()
        self._thread = None
        self.closed = False
        source.models.add(self)
        self.feed()

    @property
    def marks(self):
        """The 'squiggle' marks of the last reading; None while the setting is off."""
        self.readers.note()
        return self._marks

    def feed(self):
        """Wake the worker when the text moved on since it last looked; drop
        the marks when the setting went off. The source calls this for every
        splice (`Source.tell`)."""
        if not Toggles.TextEditor.enable_spell_check:
            if self._marks is not None:
                # Switched off: the underlines go with the next frame.
                self._marks = None
                self.readers.wake()
            self.checked = self.fed = -1
            return
        version = self.source.native.version
        if version != self.fed:
            self.fed = version
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name='editor-spelling', daemon=True)
                self._thread.start()
            self._wake.set()

    def close(self):
        self.closed = True
        self.source.models.discard(self)
        self._wake.set()

    def spans(self, text):
        """The marks of the words of one line the dictionary does not know (kept by the line's text)."""
        found = self.known.get(text)
        if found is None:
            from meltygui.editor.marks import Mark
            from meltygui.editor.spell_check import find_misspellings
            if len(self.known) > KEPT:
                self.known.clear()
            found = self.known[text] = tuple(Mark(SHAPE, low, high, COLOR) for low, high, _ in find_misspellings(text))
        return found

    def check(self):
        """Read the text as it is now and swap in its marks."""
        from meltygui.editor.marks import Marks, Lines
        from meltygui.editor import spell_check
        native = self.source.native
        version = native.version
        if version == self.checked:
            return
        spell_check._get_words(park)       # (the dictionary, the first time: read in pieces)
        lines, items = [], []
        for first in range(0, native.lines, SLICE):
            rows = native.texts(first, SLICE)
            if native.version != version:
                return self._wake.set()        # typed on meanwhile: start over once it rests
            for line, row in enumerate(rows, first):
                if row:
                    marks = self.spans(row)
                    if marks:
                        lines += [line] * len(marks)
                        items += marks
            park()
        if Toggles.TextEditor.enable_spell_check and not self.closed:
            # (Put together from the two lists as they are: no pass over every mark in one piece.)
            found = Marks(version=version)
            found.lines, found.items = Lines(lines), items
            self._marks, self.checked = found, version
            self.readers.wake()

    def _work(self):
        while not self.closed:
            self._wake.wait()
            self._wake.clear()
            # Typing restarts the wait: check once the text rests.
            while self._wake.wait(QUIET):
                self._wake.clear()
            if not self.closed and Toggles.TextEditor.enable_spell_check:
                self.check()
