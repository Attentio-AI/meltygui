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

`Menu` is the state of a list popover: every list the editor pops up (the
suggestions, an error's quick fixes, the usage picker of editor/lookup.py) is one, painted by the one `choices`
view and driven by the same keys, hover and wheel.
"""
import re
import sys
from types import SimpleNamespace

from meltygui.editor import edit
from meltygui.editor.syntax import tools

# Keywords still offered in the middle of a line.
EXPRESSION_WORDS = {'True', 'False', 'None', 'not', 'lambda', 'await'}


# Lever: rows a list popover shows at once.
ROWS = 10
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

    @property
    def open(self):
        return bool(self.rows)

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
        """An import shortcut shows the statement it would add."""
        name, kind = self.rows[at]
        return None, (self.bag._ac_import_stmts or {}).get(name, '') if kind == 'auto_import' else ''

    def refresh(self, ed, typed, explicit=False, path=None, source=None, wake=None):
        """Recompute the list for the caret. `typed`: the text just changed;
        `explicit`: asked for with Ctrl+Space; `source(near, anchor, prefix,
        dot)` replaces the built in candidates (`near` is the text around the
        caret, `anchor` the word's offset in it); `wake()` is called from the
        worker when a pending member lookup lands."""
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
            elif dot and (found := loaded_members(ed, anchor)):
                rows = helper._filter_completions(found, prefix, keep_exact=keep)
            elif dot or imports:
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


def loaded_members(ed, anchor):
    """Members of the dotted receiver before offset `anchor` when it starts
    at a module the buffer imports and this process has already loaded.
    Nothing is imported for it."""
    from meltygui.core.rendering.func_metadata import member_completions, _receiver_before
    doc = ed.doc
    near, base = doc.near(anchor)
    receiver = _receiver_before(near, anchor - base)
    if not receiver:
        return []
    root = receiver.split('.')[0]
    line = doc.line_of(anchor)
    text = doc.line(line)
    found = ed.source.bound(root, ed.back(line), len(text) - len(text.lstrip()))
    if found is None or found[0] != 'import':
        return []
    for source, name, alias in IMPORT.findall(doc.line(ed.forth(found[1])).strip()):
        if (alias or name.split('.')[0]) != root:
            continue
        module = sys.modules.get(source or (name if alias else name.split('.')[0]))
        value = getattr(module, name, None) if source else module
        if value is not None:
            try:
                return member_completions(None, receiver, globals_ns={root: value})
            except Exception:
                return []
    return []


def fix_import(ed, statement, path=None):
    """Apply one missing import quick fix. True if the text changed."""
    doc = ed.doc
    changed, text = tools()._apply_import_fix(statement, None, doc.text)
    if changed:
        edit.rewrite(doc, text)
    return changed
