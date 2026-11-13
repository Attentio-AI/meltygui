"""`Source.tree`: the code of a `Source` as a mapping that is read and written like a dict.

    source = Source(text, path)
    tree = source.tree                       # the module: its classes, functions and the names it assigns
    tree['Knobs']['gain']                    # 0.5, the literal the text assigns
    tree['Knobs']['gain'] = 0.75             # spliced into the text: every editor on the source shows it
    tree['Knobs']['scale']['parameters']['factor'] = 3.0

    draw_editor(source, state=state, ...)    # the text
    draw_any(source.tree, name='tree', ...)  # the same code as rows of keys and values, edited either way

The tree is a fact of the source like its folds: Rust reads it from one version of the text on the
source's thread (native/source/src/tree.rs) and keeps it; a `Branch` here is a path of keys that
asks for the entries it is looked at for. Nothing is copied to Python but the rows read, so a
branch costs the same in a file of 100,000 lines as in one of 100.

What is in it (the keys are the source site keys of `Source.sites`):

    name = value          the value: a number, a text, True / False, None; a `Branch` of the items of a
                          list, tuple, dict, set or call; `Code` (a str: the source text) for the rest
    class Name:           a Branch of its body
    def name(a, b=1):     a Branch: 'parameters' (the default, or EMPTY) and 'locals' (its body)
    if / for / try ..     a Branch of its body ('if##0'); an `if` holds its test as '##if'
    name(1, key=2)        a call statement ('name()'): a Branch of its arguments

Reading follows the text: an edit, by an editor or anyone, is read again once it rests (`QUIET`),
and the views that read the tree run again when the reading lands: the reading thread wakes each
by its handle (`Tree.waking`), a reading made on the asking thread wakes them through `Readers`. `source.tree` is the same object while
the keys are the same; when a key was added, removed or renamed anywhere it is another object,
which is how a retained view sees that its rows changed. Branches held from before stay good.

Writing replaces the text of one value (`Tree.put`): the entry's span, brought forward over the
splices made since its reading. The value written is what the tree answers until the reading of
that text lands, so a drag does not snap back. A `Code` is written as it is, a str quoted like the
literal it replaces, a float without float noise (`spell`). What was written as `Code` is answered
as `Code` from then on, so a text field that is typed into stays one.

Levers: `QUIET`, `INLINE`, `BATCH`, `spell`. To surface more of the code: tree.rs, `Grower`.
Not there yet: adding, removing and reordering keys; undo of a write (it is an edit from outside
for the editor's history); targets that are no single name (`self.x = 1`).
"""
import sys
import weakref

from meltygui.core.rendering.keyed import keyed_as
from meltygui.model import _source_native

KINDS = tuple(_source_native.TREE_KINDS)
LEAF = _source_native.TREE_LEAF            # kinds from here on are values, those below have entries
CODE, BLANK = KINDS.index('code'), KINDS.index('empty')
# Lever: seconds the text must rest before an edit someone else made is read into the tree.
QUIET = 0.15
# Lever: texts up to this many characters are read on the calling thread when the tree is first asked for.
INLINE = 60000
# Lever: keys fetched at once where a branch's keys are read by index (the rows of a view).
BATCH = 64


class Code(str):
    """An expression that is no literal, as its source text: `x * factor`. Written back as code."""
    __slots__ = ()

    def __repr__(self):
        return f'Code({str.__repr__(self)})'


class Empty:
    """A parameter without a default value. Writing a value gives it one."""
    __slots__ = ()

    def __repr__(self):
        return 'no default'

    def __bool__(self):
        return False


EMPTY = Empty()


def quote(value, old=''):
    """The literal of the text `value`, in the quotes of the literal it replaces where it can be."""
    text = repr(value)
    mark = old.lstrip('rRuUbBfF')[:1]
    if mark in ('"', "'") and mark != text[0] and mark not in value:
        return f'{mark}{text[1:-1]}{mark}'
    return text


def spell(value, old=''):
    """The source text of `value`; `old` is the text it replaces. To change how a type is written, add a row."""
    if isinstance(value, Code):
        return str(value)
    if isinstance(value, Branch):
        return value.text
    if isinstance(value, str):
        return quote(value, old)
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, float):
        # (15 digits: what was meant, without the noise of float arithmetic.)
        return repr(float(f'{value:.15g}')) if value - value == 0 else f"float('{value}')"
    if isinstance(value, list):
        return f"[{', '.join(map(spell, value))}]"
    if isinstance(value, tuple):
        return f"({', '.join(map(spell, value))}{',' * (len(value) == 1)})"
    if isinstance(value, dict):
        return '{' + ', '.join(f'{spell(key)}: {spell(item)}' for key, item in value.items()) + '}'
    if isinstance(value, (set, frozenset)):
        return '{' + ', '.join(map(spell, value)) + '}' if value else 'set()'
    return repr(value)


class Keys:
    """The keys of a branch in text order, read where they are asked for: `len`, an index, `.index`,
    iteration. It follows the text like its branch."""
    __slots__ = ('branch', 'first', 'held', 'stamp')

    def __init__(self, branch):
        self.branch, self.first, self.held, self.stamp = branch, 0, (), -1

    def __len__(self):
        return len(self.branch)

    def __getitem__(self, at):
        if at.__class__ is slice:
            return list(self)[at]
        branch = self.branch
        node, tree = branch.at(), branch.tree
        if at < 0:
            at += len(branch)
        if self.stamp != tree.stamp or not self.first <= at < self.first + len(self.held):
            # The keys around the one asked for: a view reads those of its rows one after another.
            self.first = max(at - BATCH // 4, 0)
            self.held = tree.source.native.tree_keys(node, self.first, BATCH) if node >= 0 else ()
            self.stamp = tree.stamp
        if not self.first <= at < self.first + len(self.held):
            raise IndexError(at)
        return self.held[at - self.first]

    def __iter__(self):
        branch = self.branch
        native, first = branch.tree.source.native, 0
        while True:
            node = branch.at()
            found = native.tree_keys(node, first, BATCH * 4) if node >= 0 else ()
            yield from found
            first += len(found)
            if len(found) < BATCH * 4:
                return

    def __contains__(self, key):
        return key in self.branch

    def index(self, key):
        node = self.branch.at()
        found = self.branch.tree.source.native.tree_find(node, key) if node >= 0 else None
        if found is None:
            raise ValueError(f'{key!r} is not a key')
        return found[1]

    def __repr__(self):
        return f'Keys({list(self)!r})'


class Branch:
    """A part of the code that has keys: the module, a class, a function, a block, a list. It is a
    path of keys into its source's tree and holds nothing of it: every answer is of the text as it
    was last read, and a branch whose code is gone is empty.

        branch[key]             the value, or the Branch, under a key (the same Branch every time)
        branch[key] = value     write the value into the text
        len, in, iteration, keys(), values(), items(), get()
        branch.kind             'module', 'class', 'def', 'parameters', 'locals', 'block', 'call', 'list', 'tuple', 'dict', 'set'
        branch.text             its source: a container's literal, a statement's header
        branch.span             (line, column, last line, end column) of that text, as of `tree.version`
        branch.plain()          a copy as plain dicts, lists and tuples
    """
    __slots__ = ('tree', 'parent', 'key', 'path', 'kids', 'node', 'stamp', '__weakref__')

    def __init__(self, tree, parent=None, key=None):
        self.tree, self.parent, self.key = tree, parent, key
        self.path = () if parent is None else parent.path + (key,)
        self.kids = {}             # key -> the Branch handed out for it
        self.node, self.stamp = -1, -1

    def at(self):
        """Its entry in the tree as last read, or -1 when the text has it no longer."""
        self.tree.sync()
        return self.entry()

    def entry(self):
        tree = self.tree
        if self.stamp != tree.stamp:
            self.stamp = tree.stamp
            if self.parent is None:
                self.node = 0 if tree.shape is not None else -1
            else:
                above = self.parent.entry()
                found = tree.source.native.tree_find(above, self.key) if above >= 0 else None
                self.node = -1 if found is None else found[0]
        return self.node

    def __getitem__(self, key):
        tree = self.tree
        node = self.at()
        tree.note()
        if tree.pending:
            write = tree.pending.get((self.path, key))
            if write is not None:
                return write[0]
        found = tree.source.native.tree_item(node, key) if node >= 0 else None
        if found is None:
            raise KeyError(key)
        entry, kind, value = found
        if tree.coded and (self.path, key) in tree.coded:
            return Code(tree.source.native.tree_text(entry)[1])
        if kind < LEAF:
            kid = self.kids.get(key)
            if kid is None:
                kid = self.kids[key] = Branch(tree, self, key)
            return kid
        return value if kind < CODE else Code(value) if kind == CODE else EMPTY

    def __setitem__(self, key, value):
        self.tree.put(self, key, value)

    def __len__(self):
        node = self.at()
        return self.tree.source.native.tree_count(node) if node >= 0 else 0

    def __contains__(self, key):
        node = self.at()
        return node >= 0 and self.tree.source.native.tree_find(node, key) is not None

    def __iter__(self):
        return iter(Keys(self))

    def keys(self):
        return Keys(self)

    def values(self):
        return (self[key] for key in Keys(self))

    def items(self):
        return ((key, self[key]) for key in Keys(self))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def found(self):
        """(kind, source text) of its entry, or None when the text has it no longer."""
        node = self.at()
        return self.tree.source.native.tree_text(node) if node >= 0 else None

    @property
    def kind(self):
        found = self.found()
        return None if found is None else KINDS[found[0]]

    @property
    def text(self):
        found = self.found()
        return '' if found is None else found[1]

    @property
    def span(self):
        node = self.at()
        found = self.tree.source.native.tree_span(node) if node >= 0 else None
        return None if found is None else found[:4]

    def plain(self):
        """A copy as plain values: a list, a tuple or a set for those, a dict for the rest."""
        kind = self.kind
        items = [item.plain() if isinstance(item, Branch) else item for item in self.values()]
        if kind in ('list', 'tuple', 'set'):
            return {'list': list, 'tuple': tuple, 'set': list}[kind](items)
        return dict(zip(Keys(self), items))

    def __repr__(self):
        return f'<{self.kind or "gone"} {".".join(map(str, self.path)) or self.tree.source.path or "source"}: {len(self)} keys>'


# How `draw_collection` reads a branch: its keys stay in Rust and cross for the rows in view.
keyed_as(Branch, keys=Branch.keys, get=Branch.__getitem__, put=Branch.__setitem__, count=len)


class Tree:
    """The tree of one `Source` (`Source.tree` is its `top`): which reading the branches answer
    from, the values written since, and who reads it. One per source, made when the tree is first
    asked for; from then on every reading of the source grows the tree too."""

    def __init__(self, source):
        self.source = weakref.proxy(source)
        self.top = Branch(self)
        self.shape = None          # the keys of the tree as last read, hashed (None: nothing read yet)
        self.version = -1          # the version of the text it was read from
        self.stamp = 0             # rises with every reading taken: the branches look their entries up again
        self.took = -1             # the source reading (`Source.stamp`) taken
        self.woke = -1             # the source reading the reading views were last woken for
        self.asked = -1            # the version of the text a reading was last asked for
        self.writing = False       # a write of the tree's own is being spliced: it is read at once
        # (path, key) -> (value, the version it was written at, its offsets in that text): answered until that text is read.
        self.pending = {}
        # The (path, key) last written as `Code`: answered as code from then on, whatever the text spells,
        # so a field being typed into stays a text field when what it holds happens to be a number.
        self.coded = set()
        self.readers = None        # `Readers`: the views that read the tree
        self.wakers = {}           # (cache id, node) -> (handle, token): how the reading thread wakes each of them

    # --- reading ---------------------------------------------------------
    def ask(self, now=False):
        """Have the text as it is read: on the source's thread, or `now` on this one."""
        self.asked = self.source.head
        self.source.ask(now=now)

    def read(self):
        """Read the text as it is on this thread and take it: for the first look at a small text, and
        for a write whose place is not known."""
        self.ask(now=True)
        self.source.land()
        self.take()

    def sync(self):
        """Take the reading that arrived, if one did. Every answer of a branch starts here."""
        source = self.source
        source.land()
        if source.stamp != self.took:
            self.take()

    def take(self):
        source = self.source
        self.took = source.stamp
        found = source.native.tree_shape()
        if found is None:
            # A reading asked for before anyone read the tree: once more, with it.
            if self.asked != source.head or self.version < 0:
                self.ask()
            return
        self.version = source.version
        self.stamp += 1
        if found[0] != self.shape:
            # Other keys: another object, so a retained view sees it. The values alone change in place.
            self.shape, self.top = found[0], Branch(self)
        if self.pending:
            self.pending = {where: write for where, write in self.pending.items() if write[1] > self.version}

    def feed(self):
        """The source calls this for every splice and every landed reading (`Source.tell`): an
        edit is read once it rests, a landed reading runs the views that read the tree."""
        source = self.source
        if source.head != self.asked:
            self.asked = source.head
            # (An editor's session asks for the edits it makes itself, in `Editor.sync`: once is enough.)
            by = source.log[-1][5] if source.log else None
            if by is None or by not in source.sessions:
                source.ask(rest=0. if self.writing else QUIET)
        if source.stamp != self.woke:
            self.woke = source.stamp
            # (A reading from the source's thread woke the views by their handles already.)
            if self.readers is not None and source.inline:
                self.readers.wake()

    def note(self):
        """The running view reads the tree: it runs again when a reading lands."""
        readers = self.readers
        if readers is None:
            if 'meltygui.core.rendering.gui_prototype' not in sys.modules:
                return             # no view can be running
            from meltygui.core.rendering.gui_readers import Readers
            readers = self.readers = Readers()
        held = len(readers.found)
        readers.note()
        if len(readers.found) != held:
            # A view that did not read it before. The reading thread wakes it by handle from the next
            # request on; a reading under way was asked for without it, so it is asked for again.
            key, cache = next(reversed(readers.found.items()))
            cache = cache()
            if cache is not None:
                self.wakers[key] = cache.waker(key[1])
                if self.version != self.source.head:
                    self.ask()

    def waking(self):
        """The (handle, token) of every view that reads the tree: what `Source.ask` hands the reading
        thread. Views that are gone are forgotten here. (Any thread may ask: nothing is iterated in place.)"""
        readers = self.readers
        if readers is None:
            return []
        for key, held in list(readers.found.items()):
            cache = held()
            if cache is None or key[1] not in cache.records:
                readers.found.pop(key, None)
                self.wakers.pop(key, None)
        return list(self.wakers.values())

    # --- writing ---------------------------------------------------------
    def where(self, path, key, entry):
        """(low, high): where the text of `entry` (the value under `key` of the branch at `path`)
        stands in the text as it is now. None when an edit made since it was read touches its ends:
        the text has to be read again before it can be replaced."""
        source = self.source
        write = self.pending.get((path, key))
        if write is not None:
            _, version, low, high = write
        else:
            version, (*_, low, high) = self.version, source.native.tree_span(entry)
        entries = source.since(version)
        if entries is None:
            return None
        for _, _, at, gone, came, _ in entries:
            end = at + gone
            if end < low or (end == low and gone):
                low, high = low + came - gone, high + came - gone
            elif (at == low and end == high and gone) or (low < at and end < high):
                high += came - gone                 # the value itself was replaced, or edited inside
            elif not (at > high or (at == high and gone)):
                return None
        return low, high

    def put(self, branch, key, value):
        """Write `value` in place of the value under `key` of `branch`: one splice of the source, which
        every editor on it follows. The tree answers `value` there until that text is read."""
        source, native = self.source, self.source.native
        for again in (False, True):
            node = branch.at()
            found = native.tree_find(node, key) if node >= 0 else None
            where = None if found is None else self.where(branch.path, key, found[0])
            if where is not None:
                break
            if again or (found is None and self.version == source.head):
                raise KeyError(f'{key!r} is not in {branch!r}: a write replaces a value the code has (adding a key is not there yet)')
            self.read()            # the text moved under it: look again in the text as it is
        low, high = where
        kind, _ = native.tree_text(found[0])
        old = native.slice(low, high)
        # (A parameter without a default: the place after it takes `=value`.)
        text = ('=' if kind == BLANK else '') + spell(value, old[1:] if kind == BLANK else old)
        if text == old:
            return
        self.writing = True
        try:
            source.splice(low, high, text)
        finally:
            self.writing = False
        self.pending[(branch.path, key)] = (value, source.head, low, low + len(text))
        (self.coded.add if isinstance(value, Code) else self.coded.discard)((branch.path, key))
