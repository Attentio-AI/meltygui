"""Code suggestions, signature hints and import fixes for an `Editor`.

The ranking, snippet, member and import logic is the pure code of
`text_editor.py`; this module only sequences it over a `Document`. The legacy
helpers keep their caches on whatever object they are given: `Bag` is that
object here. The candidates are the names the session's `Source` found
bound around the caret (`Editor.names`); helpers that read around the caret
are given a window of the text (`Document.near`). The whole text is joined
only for what leaves the process or rewrites the file: a member or signature
lookup that has no answer yet (jedi reads the file), an import statement
being added.

A row of the suggestion list shows more than its name (`Suggest.extra`): the
tint its symbol states and, for a function or a method, its parameters.
Both are read for the rows on screen only and kept (`Suggest.told`): from the
source's facts and the editor's tint marks for a name the text defines, from
the loaded object for a member or an imported name. Only the tints of a
receiver's members come from the roster's tables, which are asked for on a
pool thread once per list (`tinted`).

`Menu` is the state of a list popover: every list the editor pops up (the
suggestions, an error's quick fixes, the usage picker of editor/lookup.py) is one, painted by the one `choices`
view and driven by the same keys, hover and wheel.
"""
import builtins
import re
import sys
from types import SimpleNamespace

from meltygui.editor import edit
from meltygui.editor.syntax import tools

# Keywords still offered in the middle of a line.
EXPRESSION_WORDS = {'True', 'False', 'None', 'not', 'lambda', 'await'}


# Lever: rows a list popover shows at once.
ROWS = 10
# Lever: characters of a row's parameter list before it is cut.
HINT = 40
# Loaded object -> its parameter list as a row shows it: reading a signature costs about 50 us, and a
# list of builtins would read ten for every word typed. Lever: `KEPT` entries, then it starts over.
SIGNED, KEPT = {}, 4096
MISS = object()
# The kinds of row that may be called: they get a parameter list.
CALLED = frozenset(('function', 'method', 'builtin'))
WORDS = []


def words():
    """Builtins and keywords as candidates: the lowest in rank."""
    if not WORDS:
        import builtins
        import keyword
        WORDS.extend((name, 'builtin') for name in dir(builtins) if not name.startswith('_'))
        WORDS.extend((word, 'kw') for word in keyword.kwlist if len(word) > 1)
    return WORDS


class Menu:
    """One list popover: `rows` are (text, tag), `index` the selected row,
    `first` the first one shown, `picked` a row the pointer chose (the owner
    acts on it and clears it). `extra` gives a row its tint and dim suffix.
    `cells`: the popover's width in characters (None: measured from the rows
    on every run, which a long list sets once instead)."""

    def __init__(self):
        self.rows, self.index, self.first, self.picked, self.cells = [], 0, 0, None, None
        self.tick = 0              # rises when what `extra` gives changed without the rows changing

    @property
    def open(self):
        return bool(self.rows)

    def wide(self):
        """The popover's width in characters when `cells` does not say: its widest row with its suffix."""
        return max(len(text) + len(tag) + (len(self.extra(at)[1]) + 2 if self.extra(at)[1] else 0)
                   for at, (text, tag) in enumerate(self.rows))

    def set(self, rows):
        """Show `rows`, keeping the selection where it can stay."""
        self.rows, self.picked = rows, None
        self.index = min(self.index, max(len(rows) - 1, 0))
        self.show()

    def show(self):
        """Keep the selected row among the ones shown."""
        self.first = min(max(self.first, self.index - ROWS + 1), self.index, max(len(self.rows) - ROWS, 0))

    def step(self, delta):
        """Move the selection `delta` rows, around the ends."""
        if self.rows:
            self.index = (self.index + delta) % len(self.rows)
            self.show()

    def page(self, delta):
        """Move the selection `delta` pages, stopping at the ends."""
        if self.rows:
            self.index = min(max(self.index + delta * ROWS, 0), len(self.rows) - 1)
            self.show()

    def scroll(self, rows):
        """The wheel: move the shown rows, not the selection."""
        self.first = min(max(self.first + rows, 0), max(len(self.rows) - ROWS, 0))

    def point(self, row):
        """The pointer is over shown row `row`: it becomes the selection."""
        if self.rows:
            self.index = min(max(self.first + row, 0), len(self.rows) - 1)

    def extra(self, at):
        """(tint, suffix) of row `at`: an (r, g, b) for its text and dim text after it."""
        return None, ''


class Bag:
    """Attribute store for the legacy completion helpers."""
    text_cursor_pos = 0
    base = 0           # where the window the helpers are given starts in the text
    site = None        # the armed snippet trigger: (offset in the text, trigger)

    @property
    def _ac_snip_site(self):
        """The armed trigger as the snippet helper keeps it: by offset in its window."""
        return self.site and (self.site[0] - self.base, self.site[1])

    @_ac_snip_site.setter
    def _ac_snip_site(self, found):
        self.site = found and (found[0] + self.base, found[1])

    def invalidate_up(self):
        pass


class Suggest(Menu):
    """The suggestion list: `rows` are (name, kind)."""

    def __init__(self):
        super().__init__()
        self.bag = Bag()
        self.anchor, self.prefix = 0, ''
        self.import_key = self.imports = None
        self.explicit = False
        self.pending = None        # a member or signature lookup still running
        self.signature = None      # (name, parameters, active index)
        # What a session remembers between keys, all as offsets in the text:
        self.stops = []            # the accepted snippet's tab stops still to visit, counted from the text's end
        self.quiet = None          # the completion site Escape closed the list at: closed until the caret leaves it
        self.call = None           # the call (its paren) the signature hint shows for
        self.hushed = None         # the call whose hint Escape dismissed
        self.owed = False          # the text was edited while the list hid the hint: the next `hint` counts as edited
        # What the rows show besides their names (`extra`), per list: a list is one site (`anchor`) and receiver.
        self.listed = None         # (anchor, receiver) the three below belong to
        self.tints = {}            # member name -> (r, g, b) its entry states, once `tinted` answered
        self.notes = {}            # name -> (tint, parameter list), as read so far (`told`)
        self.scope = None          # (the editor, the caret's line, the receiver's loaded object or None, that line in the
                                   # landed reading, its indent): what `told` reads
        self.sized = (None, 0)     # (the rows `wide` was last measured for, its answer)

    def close(self):
        self.rows, self.explicit, self.first, self.picked = [], False, 0, None
        self.bag._ac_snip_site = None

    def drop(self):
        """Leave the fill-in flow, as a click does: the list closes, the
        snippet's tab stops are abandoned and the hint is disarmed until the
        next edit inside a call."""
        self.close()
        self.stops, self.call, self.signature, self.owed = [], None, None, False

    def escape(self, ed):
        """Escape drops all of it, and more: the list stays closed at this
        site and the hint for the call around the caret stays away, until
        the caret leaves them or Ctrl+Space / Ctrl+P ask."""
        if self.open:
            self.quiet = self.anchor
        near, base = ed.doc.near(ed.doc.caret)
        paren = tools()._call_context(near, ed.doc.caret - base)[0]
        self.hushed = None if paren is None else paren + base
        self.drop()

    def leave(self, ed):
        """The caret moved without an edit: an Escape at a site it left is forgotten."""
        if self.quiet is not None:
            near, base = ed.doc.near(ed.doc.caret)
            if tools()._completion_context(near, ed.doc.caret - base)[1] + base != self.quiet:
                self.quiet = None

    def hop(self, doc):
        """Tab after a snippet: the caret goes to its next tab stop. False when none is left."""
        if not self.stops:
            return False
        doc.select(doc.size - self.stops.pop(0))
        return True

    def arrived(self):
        """The pending lookup finished. Returns whether it was the members' (the list refreshes), not a signature's."""
        members = self.pending is getattr(self.bag, '_ac_jedi_future', None)
        self.pending = None
        return members

    def extra(self, at):
        """(tint, suffix) of row `at`: the tint its symbol states; the
        statement an import shortcut would add, or a function's parameters."""
        name, kind = self.rows[at]
        if kind == 'auto_import':
            return None, (self.bag._ac_import_stmts or {}).get(name, '')
        if kind == 'snip' or self.scope is None:
            return None, ''         # (a snippet's label names no symbol)
        found = self.notes.get(name)
        if found is None:
            found = self.notes[name] = self.told(name, kind in CALLED)
        return self.tints.get(name) or found[0], found[1]

    def wide(self):
        """As `Menu.wide`, without reading a parameter list for it: a list that has functions leaves `HINT` cells."""
        if self.sized[0] is not self.rows:
            statements = self.bag._ac_import_stmts or {}
            self.sized = (self.rows, max(
                len(name) + len(kind) + (len(statements.get(name, '')) + 2 if kind == 'auto_import' else HINT if kind in CALLED else 0)
                for name, kind in self.rows))
        return self.sized[1]

    def told(self, name, called):
        """(tint, parameters) of the symbol a row names. For a name the text
        defines: the tint its definition's line carries (the editor's 'tints'
        marks) and, of a `def`, the source's parameter facts. Else, when it
        may be `called`, the parameters of the loaded object (the receiver's
        member, an imported name, a builtin) as `(a, b, *, c)`; '' when it
        is not known to be called."""
        ed, line, owner, landed, indent = self.scope
        value = MISS
        if owner is not None:
            # (Only a row tagged as a method is read off the object: reading any other could run a property.)
            try:
                value = getattr(owner, name, MISS) if called else MISS
            except Exception:
                pass
        else:
            bound = ed.source.bound(name, landed, indent)
            if bound is not None and bound[0] in ('def', 'class'):
                mark = ed.mark('tints', ed.forth(bound[1]), 'tint')
                return mark and mark.color, cut([found[0] for found in ed.source.parameters(bound[1])]) if bound[0] == 'def' else ''
            if called:
                value = imported(ed, name, line) if bound is not None and bound[0] == 'import' else \
                    getattr(builtins, name, MISS) if bound is None else MISS
        if not called or value is MISS or isinstance(value, type) or not callable(value):
            return None, ''
        try:
            found = SIGNED.get(value)
        except TypeError:
            return None, '(...)'     # (an unhashable callable: not worth a signature per frame)
        if found is None:
            try:
                from meltygui.code import code_checks
                spec = code_checks._live_spec(value, name)
            except Exception:
                spec = None
            if len(SIGNED) >= KEPT:
                SIGNED.clear()
            found = SIGNED[value] = '(...)' if spec is None else cut(
                spec.named + ['*args'] * spec.has_var_pos + ['*'] * bool(spec.kwonly and not spec.has_var_pos)
                + spec.kwonly + ['**kwargs'] * spec.has_var_kw)
        return None, found

    def describe(self, ed, anchor, receiver, owner, path, roster, wake):
        """A list is shown at `anchor` for the members of `receiver` ('': the names in sight). What its
        rows show besides their names starts over for another list, and its tints are asked for."""
        # (The caret's line as the landed reading has it, and its indent: asked once for the list, not per row.)
        line = ed.doc.line_of(ed.doc.caret)
        text = ed.doc.line(line)
        self.scope = (ed, line, owner, ed.back(line), len(text) - len(text.lstrip()))
        if (anchor, receiver) == self.listed:
            return
        self.listed, self.tints, self.notes = (anchor, receiver), {}, {}
        if not (receiver and path):
            return
        from meltygui.core.runtime.background import Background

        def landed(done, listed=self.listed):
            # (Pool thread. Another list was opened meanwhile: its own answer is on the way.)
            found = done.result() if not done.cancelled() and done.exception() is None else None
            if found and listed == self.listed:
                self.tints = found
                self.tick += 1
                if wake is not None:
                    wake()
        Background.submit_io(tinted, roster, path, receiver).add_done_callback(landed)

    def refresh(self, ed, typed, explicit=False, path=None, source=None, wake=None, roster=None):
        """Recompute the list for the caret. `typed`: the text just changed;
        `explicit`: asked for with Ctrl+Space; `source(near, anchor, prefix,
        dot)` replaces the built in candidates (`near` is the text around the
        caret, `anchor` the word's offset in it); `wake()` is called from the
        worker when a pending member lookup or the rows' tints land;
        `roster`: the project's, for the tints other files state."""
        helper, bag, doc = tools(), self.bag, ed.doc
        caret = doc.caret
        near, bag.base = doc.near(caret)
        base, at = bag.base, caret - bag.base
        self.explicit = explicit or (self.explicit and self.open)
        keep = self.explicit
        address = SimpleNamespace(path=path, start=None) if path else None
        was = self.prefix if self.open else None
        bag.text_cursor_pos = caret
        snippet = helper._snippet_context(bag, near, at, typed)
        if snippet is not None and snippet[1]:
            wanted = snippet[1].lower()
            if not any(wanted in item.label.lower() or wanted in item.insert.replace('$0', '').lower()
                       for item in snippet[2]):
                bag._ac_snip_site = snippet = None
        # Escape closed the list at `quiet`: typing there does not open it again, Ctrl+Space does.
        site = snippet and snippet[0] + base
        if explicit:
            self.quiet = None
        elif site is not None and site == self.quiet:
            snippet = None
        bag._ac_snips = bag._ac_import_stmts = None
        receiver, owner = '', None
        if snippet is not None:
            anchor, prefix = snippet[0] + base, snippet[1]
            bag._ac_snips = {item.label: item for item in snippet[2]}
            wanted = prefix.lower()
            rows = [(item.label, 'snip') for item in snippet[2]
                    if (wanted in item.label.lower() or wanted in item.insert.replace('$0', '').lower())
                    and not (wanted and item.insert.replace('$0', '').lower().endswith(wanted))]
        else:
            prefix, anchor, dot = helper._completion_context(near, at)
            imports = helper._import_line_context(near, anchor)
            anchor += base
            rows = []
            if not (prefix or dot or keep) or (ed.syntax.quiet(*doc.place(caret)) and not keep) or anchor == self.quiet:
                pass
            elif not typed and not keep and not self.open:
                pass
            elif source is not None:
                rows = helper._filter_completions(source(near, anchor - base, prefix, dot) or [], prefix, keep_exact=keep)
            elif dot and (found := loaded_members(ed, anchor))[0]:
                rows = helper._filter_completions(found[0], prefix, keep_exact=keep)
                receiver, owner = found[1:]
            elif dot or imports:
                from meltygui.core.rendering.func_metadata import _receiver_before
                receiver = (_receiver_before(near, anchor - base) or '') if dot else ''
                # The lookup is keyed by its line and receiver: once it has an answer the text is not read again.
                line = doc.line_of(anchor)
                key = (line, doc.slice(doc.start(line), anchor))
                if getattr(bag, '_ac_jedi_done_key', None) == key and bag._ac_jedi_members:
                    bag._ac_member_tints = getattr(bag, '_ac_jedi_member_tints', None)
                    members, waiting = bag._ac_jedi_members, False
                else:
                    members, waiting = helper._ensure_member_completions(bag, doc.text, anchor, address)
                future = getattr(bag, '_ac_jedi_future', None)
                if waiting and future is not None and future is not self.pending and wake is not None:
                    self.pending = future
                    future.add_done_callback(lambda done: wake())
                rows = helper._filter_completions(members or [], prefix, keep_exact=keep)
            elif not keep and helper._defining_keyword_before(near, anchor - base):
                pass        # after `def`, `class`, `for`, `as`, `lambda` a new name is being made up: nothing to offer
            else:
                line = doc.line_of(caret)
                found = ed.names(line, prefix)
                seen = {name for name, _ in found}
                # (The import shortcuts read the pool to leave names the scope already has alone.)
                bag._ac_pool = found + [row for row in words() if row[0] not in seen]
                rows = helper._filter_completions(bag._ac_pool, prefix, keep_exact=keep)
                if doc.slice(doc.start(line), anchor).strip():
                    rows = [(name, kind) for name, kind in rows if kind != 'kw' or name in EXPRESSION_WORDS]
                # A buffer that is its whole file has every module-level name in the pool. Only a part of
                # a file needs the file's own table for them, which the legacy roster reads on this thread.
                whole = ed.numbers.table is None and ed.numbers.first == 1
                rows = self.shortcuts(rows, prefix, anchor, None if whole else address, keep)
        if prefix != was:
            self.index = self.first = 0
        if self.quiet not in (anchor, site):
            self.quiet = None       # the caret left the site Escape closed
        self.anchor, self.prefix = anchor, prefix
        self.set(rows)
        if not rows:
            self.explicit = False
        elif snippet is None:
            self.describe(ed, anchor, receiver, owner, path, roster, wake)

    def shortcuts(self, rows, prefix, anchor, address, keep):
        """Merge import shortcut rows. The legacy merge rescans the loaded
        modules on every call, so it runs once per word; further letters
        narrow its result here."""
        bag = self.bag
        if self.import_key != (anchor, keep) or len(prefix) < 3:
            rows = tools()._ac_import_rows(bag, rows, prefix, address, explicit=keep)
            self.import_key = (anchor, keep) if len(prefix) >= 3 or keep else None
            self.imports = bag._ac_import_stmts
            return rows
        statements = self.imports
        if not statements:
            return rows
        bag._ac_import_stmts = statements
        wanted, have = prefix.lower(), {name for name, _ in rows}
        rows = [(name, 'auto_import' if kind == 'name' and name in statements else kind) for name, kind in rows]
        return rows + [(name, 'auto_import') for name in statements
                       if name not in have and name != prefix and name.lower().startswith(wanted)]

    def accept(self, ed, path=None):
        """Insert the picked row: a name, a snippet, and its import when the
        row is an import shortcut."""
        helper, bag, doc = tools(), self.bag, ed.doc
        pick = self.rows[self.index][0]
        near, base = doc.near(doc.caret)
        caret, anchor = doc.caret - base, self.anchor - base
        stop = helper._completion_replace_end(near, caret, pick, bag._ac_snips)
        insert, offset, stops = helper._ac_pick_insert(
            bag, pick, following=near[stop:stop + 64], preceding=near[max(0, anchor - 64):anchor],
            replaced=near[anchor:stop], line_prefix=near[near.rfind('\n', 0, anchor) + 1:anchor])
        doc.replace(base + anchor, base + stop, insert, caret=base + anchor + offset)
        # The snippet's further `$N` stops, from the text's end: filling in an earlier one does not move them.
        self.stops = [doc.size - (base + anchor + at) for at in stops]
        bag.text_cursor_pos = doc.caret
        if (bag._ac_import_stmts or {}).get(pick):
            # An import shortcut: its statement goes to the top of the text.
            address = SimpleNamespace(path=path, start=None) if path else None
            imported = helper._ac_apply_auto_import(bag, pick, address, doc.text)
            if imported != doc.text:
                edit.rewrite(doc, imported, bag.text_cursor_pos)
        self.close()
        return pick

    def hint(self, ed, path=None, wake=None, edited=False, asked=False):
        """The call around the caret as (name, parameters, active index), or
        None. It shows while the call is edited (`edited`: the text just
        changed) or once asked for (`asked`: Ctrl+P), and stays until the
        caret leaves that call's parentheses: a caret that only moved or
        clicked into a call shows none. Escape hushes it for the call."""
        helper, bag, doc = tools(), self.bag, ed.doc
        caret = doc.caret
        near, base = doc.near(caret)
        paren, active = helper._call_context(near, caret - base)
        call = None if paren is None else paren + base
        edited, self.owed = edited or self.owed, False
        if asked or call != self.hushed:
            self.hushed = None
        if call is None or call == self.hushed:
            self.call = None
        elif edited or asked:
            self.call = call
        elif call != self.call:
            self.call = None
        self.signature = None
        if self.call is None or not helper._callee_at(near, paren):
            return None
        address = SimpleNamespace(path=path, start=None) if path else None
        # The lookup is keyed by its paren and callee: once it has an answer the text is not read again.
        if getattr(bag, '_ac_sig_done_key', None) == (paren + base, helper._callee_at(near, paren)):
            found = bag._ac_sig_data
        else:
            found = helper._ensure_signature_help(bag, doc.text, paren + base, caret, address)
        future = getattr(bag, '_ac_sig_future', None)
        if found is None and future is not None and future is not self.pending and wake is not None:
            self.pending = future
            future.add_done_callback(lambda done: wake())
        if found:
            self.signature = (found[0], found[1], active)
        return self.signature


IMPORT = re.compile(r'^(?:from\s+([\w.]+)\s+)?import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$', re.M)


def cut(names):
    """A parameter list as a row shows it: without `self`, no longer than `HINT`."""
    found = '(' + ', '.join(names[1:] if names[:1] in (['self'], ['cls']) else names) + ')'
    return found if len(found) <= HINT else found[:HINT - 4] + '...)'


def imported(ed, root, line):
    """What the name `root`, as seen from `line`, is in this process when the
    buffer imports it from a module that is already loaded, else None.
    Nothing is imported for it."""
    doc = ed.doc
    text = doc.line(line)
    found = ed.source.bound(root, ed.back(line), len(text) - len(text.lstrip()))
    if found is None or found[0] != 'import':
        return None
    for source, name, alias in IMPORT.findall(doc.line(ed.forth(found[1])).strip()):
        if (alias or name.split('.')[0]) == root:
            module = sys.modules.get(source or (name if alias else name.split('.')[0]))
            return getattr(module, name, None) if source else module
    return None


def loaded_members(ed, anchor):
    """(rows, receiver, its object) for the dotted receiver before offset
    `anchor` when it starts at a module the buffer imports and this process
    has already loaded; ([], '', None) otherwise."""
    from meltygui.core.rendering.func_metadata import member_completions, _receiver_before
    doc = ed.doc
    near, base = doc.near(anchor)
    receiver = _receiver_before(near, anchor - base)
    root, *rest = receiver.split('.') if receiver else ['']
    value = imported(ed, root, doc.line_of(anchor)) if root else None
    if value is None:
        return [], '', None
    try:
        rows = member_completions(None, receiver, globals_ns={root: value})
        for part in rest:
            value = getattr(value, part)
    except Exception:
        return [], '', None
    return rows, receiver, value


def tinted(roster, path, receiver):
    """{name: (r, g, b)} of the tints the members of `receiver` state, as
    seen from file `path`: the entry the receiver names in the project and
    that entry's children. `roster`: the caller's, else the one Melty keeps
    for the file's project when a model made it (none is made for this).
    Pool thread: the roster reads a file's whole table."""
    import os
    if roster is None:
        from meltygui.code.source_context import analysis_project
        from meltygui.core.melty import Melty
        roster = Melty.rosters.get(analysis_project(None, path).root)
    found = roster.resolve(os.path.realpath(path), receiver) if roster is not None else None
    if found is None:
        return {}
    owner = found[1] + '.'
    return {row[1]: tuple(row[9][:3]) for row in roster.entries(found[0])
            if len(row[9]) >= 3 and row[0].startswith(owner) and '.' not in row[0][len(owner):]}


def fix_import(ed, statement, path=None):
    """Apply one missing import quick fix. True if the text changed."""
    doc = ed.doc
    changed, text = tools()._apply_import_fix(statement, None, doc.text)
    if changed:
        edit.rewrite(doc, text)
    return changed
