"""Search for an `Editor`: the find bar, the current hit, next and previous.

The hits of a term are the source's (`Editor.find` asks, `Editor.land`
takes them as the 'hits' set: a `marks.Hits` kept in Rust). This module adds
what a search session is besides them: which hit is the current one (the
one mark of the 'found' set, so it stays on its text through edits),
stepping through the hits with wrap-around (`Find.step`: two bisections in
`Hits`, never a pass over them), and the find bar's field. As in the legacy
editor a term is matched case blind, the caret and the selection stay where
they are, and landing on a hit opens the folds over it, brings it into view
and flashes it.

The bar is the editor's own: Ctrl+F opens it (with the selection as the
term), the keyboard is then the field's (`Find.typed`), Enter and Down step
on, Shift+Enter and Up back, Escape closes. The field is a single line
session of its own; the view paints it (`finder` in view/editor_view.py). A
caller with a field of its own passes `draw_editor(search=)` and steps with
`state.pane.find(1)` / `find(-1)`; `state.pane.search` is the term, the
current hit's number and the count for either. To give the bar another key,
add it in `Find.typed`.
"""
from collections import namedtuple

import meltygui.core.windowing.window_constants as glfw

from meltygui.editor import keys
from meltygui.editor.marks import Mark, Marks

# Lever: characters of a selection Ctrl+F takes as the term.
LONGEST = 200
# What a caller reads of a search: the term, the current hit counted from 1 (0: none), the hits in all.
Search = namedtuple('Search', 'term index count')


class Find:
    def __init__(self):
        self.open = False          # the bar is shown
        self.focused = False       # the keyboard is the field's
        self.field = None          # the term's own single line session, made when the bar first opens
        self.owed = False          # the term changed: once its hits are in, the first from the caret is the current one
        self.clicked = None        # what the pointer did in the bar: 'close', or the column pressed in the field

    # --- the current hit ---------------------------------------------------
    def current(self, ed):
        """(line, column) of the current hit, or None."""
        found = ed.marks.get('found')
        rows = found.on(0, ed.doc.lines) if found is not None else ()
        return (rows[0][0], rows[0][1].low) if rows else None

    def state(self, ed):
        """The `Search` a caller reads (`Pane.search`)."""
        hits, here = ed.marks.get('hits'), self.current(ed)
        count = len(hits) if hits is not None else 0
        at = hits.seek(*here) if here is not None and count else count
        return Search(ed.term, at + 1 if at < count and hits.at(at)[:2] == here else 0, count)

    def step(self, ed, delta):
        """Make the next (`delta` 1) or the previous (-1) hit the current
        one, around the ends; without a current one the nearest from the
        caret (0: the first at or after it). The view reveals it
        (`ed.landing`). Returns whether there is one."""
        hits = ed.marks.get('hits')
        count = len(hits) if hits is not None else 0
        if not count:
            ed.put('found', None)
            return False
        here = self.current(ed)
        if here is None:
            at = hits.seek(*ed.doc.place(ed.doc.caret)) - (delta < 0)
        else:
            at = hits.seek(*here)
            # (An edit may have taken the current hit: then the next one is the one that stands there now.)
            at += delta if at < count and hits.at(at)[:2] == here else -(delta < 0)
        line, low, high = hits.at(at % count)
        ed.put('found', Marks([(line, Mark('found', low, high))]))
        ed.landing = (ed.doc.index(line, low), ed.doc.index(line, high), True)
        if ed.wake is not None:
            ed.wake()
        return True

    def settle(self, ed):
        """Every run, after the source's readings landed: a new term's first hit, once its hits are in."""
        if self.owed and ed.marks.get('hits') is not None:
            self.owed = False
            self.step(ed, 0)

    def seek(self, ed, term):
        """Search for `term` (the bar's or the caller's): its hits are asked for, the current one is owed."""
        ed.find(term)
        ed.put('found', None)
        self.owed = bool(term)

    # --- the bar -----------------------------------------------------------
    def show(self, ed):
        """Ctrl+F: open the bar and give its field the keyboard, the term
        selected (typing replaces it). A selection inside one line is the term."""
        if self.field is None:
            from meltygui.editor.editor import Editor
            self.field = Editor('', None, False)
            self.field.doc.record = None       # the term is no undo step
        doc, field = ed.doc, self.field.doc
        low, high = doc.selection
        if 0 < high - low <= LONGEST and doc.line_of(low) == doc.line_of(high):
            field.splice(0, field.size, doc.slice(low, high))
        field.select(field.size, 0)
        self.open = self.focused = True
        self.follow(ed)

    def close(self, ed):
        """Escape or the close button: the bar and the highlights go, the
        term stays for the next Ctrl+F, the keyboard is the text's again. A
        caller's own `search=` term is shown again by the view's next run."""
        self.open = self.focused = self.owed = False
        ed.find('')
        ed.put('found', None)
        ed.sources.pop('search', None)
        if ed.wake is not None:
            ed.wake()

    def follow(self, ed):
        """The field's text is the term."""
        text = self.field.doc.text
        if text != ed.term:
            self.seek(ed, text)

    def press(self, ed):
        """What the pointer did in the bar since the last run. Returns whether the field took the keyboard."""
        clicked, self.clicked = self.clicked, None
        if clicked == 'close':
            self.close(ed)
        elif clicked is not None:
            self.focused = True
            self.field.doc.select(clicked)
        return clicked is not None and self.focused

    def typed(self, ed, pressed, text, desk):
        """The key presses (`event.keys()`) and the typed text (`event.text()`) of one run, while the
        field has the keyboard."""
        field = self.field
        pending = text
        own = keys.Desk(True, True, None, desk.copy, desk.clipboard)
        for key, mods in pressed:
            chord = mods & keys.MODS
            if keys.printable(key, mods):
                if pending:
                    field.doc.replace(*field.doc.selection, pending[0])
                    pending = pending[1:]
            elif key == glfw.KEY_ESCAPE:
                return self.close(ed)
            elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
                if not chord & ~keys.SHIFT:
                    self.step(ed, -1 if chord else 1)
            elif key in (glfw.KEY_UP, glfw.KEY_DOWN) and not chord:
                self.step(ed, -1 if key == glfw.KEY_UP else 1)
            elif key == glfw.KEY_F and chord == keys.CTRL:
                field.doc.select(field.doc.size, 0)
            else:
                keys.press(field, own, key, mods)
        if pending:
            field.doc.replace(*field.doc.selection, pending.split('\n')[0])
        self.follow(ed)
