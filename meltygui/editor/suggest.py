"""Code suggestions, signature hints and import fixes for an `Editor`.

The ranking, snippet, member and import logic is the pure code of
`text_editor.py`; this module only sequences it over a `Document`. The legacy
helpers keep their caches on whatever object they are given: `Bag` is that
object here.

`Menu` is the state of a list popover: every list the editor pops up (the
suggestions, an error's quick fixes) is one, painted by the one `choices`
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


class Menu:
    """One list popover: `rows` are (text, tag), `index` the selected row,
    `first` the first one shown, `picked` a row the pointer chose (the owner
    acts on it and clears it). `extra` gives a row its tint and dim suffix."""

    def __init__(self):
        self.rows, self.index, self.first, self.picked = [], 0, 0, None

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

    def invalidate_up(self):
        pass


class Suggest(Menu):
    """The suggestion list: `rows` are (name, kind)."""

    def __init__(self):
        super().__init__()
        self.bag = Bag()
        self.anchor, self.prefix = 0, ''
        self.pool_key = self.import_key = self.imports = None
        self.explicit = False
        self.pending = None        # a member lookup still running
        self.signature = None      # (name, parameters, active index)

    def close(self):
        self.rows, self.explicit, self.first, self.picked = [], False, 0, None
        self.bag._ac_snip_site = None

    def extra(self, at):
        """An import shortcut shows the statement it would add."""
        name, kind = self.rows[at]
        return None, (self.bag._ac_import_stmts or {}).get(name, '') if kind == 'auto_import' else ''

    def refresh(self, ed, typed, explicit=False, tree=None, path=None, source=None, wake=None):
        """Recompute the list for the caret. `typed`: the text just changed;
        `explicit`: asked for with Ctrl+Space; `source(text, anchor, prefix,
        dot)` replaces the built in candidates; `wake()` is called from the
        worker when a pending member lookup lands."""
        helper, bag, doc = tools(), self.bag, ed.doc
        text, caret = doc.text, doc.caret
        self.explicit = explicit or (self.explicit and self.open)
        keep = self.explicit
        address = SimpleNamespace(path=path, start=None) if path else None
        was = self.prefix if self.open else None
        bag.text_cursor_pos = caret
        snippet = helper._snippet_context(bag, text, caret, typed)
        if snippet is not None and snippet[1]:
            wanted = snippet[1].lower()
            if not any(wanted in item.label.lower() or wanted in item.insert.replace('$0', '').lower()
                       for item in snippet[2]):
                bag._ac_snip_site = snippet = None
        bag._ac_snips = bag._ac_import_stmts = None
        if snippet is not None:
            anchor, prefix = snippet[0], snippet[1]
            bag._ac_snips = {item.label: item for item in snippet[2]}
            wanted = prefix.lower()
            rows = [(item.label, 'snip') for item in snippet[2]
                    if (wanted in item.label.lower() or wanted in item.insert.replace('$0', '').lower())
                    and not (wanted and item.insert.replace('$0', '').lower().endswith(wanted))]
        else:
            prefix, anchor, dot = helper._completion_context(text, caret)
            imports = helper._import_line_context(text, anchor)
            rows = []
            if not (prefix or dot or keep) or (ed.syntax.quiet(doc, caret) and not keep):
                pass
            elif not typed and not keep and not self.open:
                pass
            elif source is not None:
                rows = helper._filter_completions(source(text, anchor, prefix, dot) or [], prefix, keep_exact=keep)
            elif dot and loaded_members(text, anchor):
                rows = helper._filter_completions(loaded_members(text, anchor), prefix, keep_exact=keep)
            elif dot or imports:
                members, waiting = helper._ensure_member_completions(bag, text, anchor, address)
                future = getattr(bag, '_ac_jedi_future', None)
                if waiting and future is not None and future is not self.pending and wake is not None:
                    self.pending = future
                    future.add_done_callback(lambda done: wake())
                rows = helper._filter_completions(members or [], prefix, keep_exact=keep)
            else:
                line = doc.line_of(caret)
                key = (id(tree), line, anchor)
                if key != self.pool_key:
                    bag._ac_pool = helper._completion_pool(tree, text, line)
                    self.pool_key = key
                rows = helper._filter_completions(bag._ac_pool, prefix, keep_exact=keep)
                if text[doc.start(line):anchor].strip():
                    rows = [(name, kind) for name, kind in rows if kind != 'kw' or name in EXPRESSION_WORDS]
                rows = self.shortcuts(rows, prefix, anchor, address, keep)
        if prefix != was:
            self.index = self.first = 0
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
        text, caret, anchor = doc.text, doc.caret, self.anchor
        stop = helper._completion_replace_end(text, caret, pick, bag._ac_snips)
        insert, offset, _ = helper._ac_pick_insert(
            bag, pick, following=text[stop:stop + 64], preceding=text[max(0, anchor - 64):anchor],
            replaced=text[anchor:stop], line_prefix=text[doc.start(doc.line_of(anchor)):anchor])
        doc.replace(anchor, stop, insert, caret=anchor + offset)
        bag.text_cursor_pos = doc.caret
        address = SimpleNamespace(path=path, start=None) if path else None
        imported = helper._ac_apply_auto_import(bag, pick, address, doc.text)
        if imported != doc.text:
            edit.rewrite(doc, imported, bag.text_cursor_pos)
        self.close()
        return pick

    def hint(self, ed, path=None, wake=None):
        """The call around the caret as (name, parameters, active index), or None."""
        helper, bag, doc = tools(), self.bag, ed.doc
        text, caret = doc.text, doc.caret
        paren, active = helper._call_context(text, caret)
        self.signature = None
        if paren is None or not helper._callee_at(text, paren):
            return None
        address = SimpleNamespace(path=path, start=None) if path else None
        found = helper._ensure_signature_help(bag, text, paren, caret, address)
        future = getattr(bag, '_ac_sig_future', None)
        if found is None and future is not None and future is not self.pending and wake is not None:
            self.pending = future
            future.add_done_callback(lambda done: wake())
        if found:
            self.signature = (found[0], found[1], active)
        return self.signature


IMPORT = re.compile(r'^(?:from\s+([\w.]+)\s+)?import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$', re.M)


def loaded_members(text, anchor):
    """Members of the dotted receiver before `anchor` when it starts at a
    module the buffer imports and this process has already loaded. Nothing is
    imported for it."""
    from meltygui.core.rendering.func_metadata import member_completions, _receiver_before
    receiver = _receiver_before(text, anchor)
    if not receiver:
        return []
    root = receiver.split('.')[0]
    for source, name, alias in IMPORT.findall(text):
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
