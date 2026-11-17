"""Scope: the code of a class, a function or a module. What `Source(thing)` returns.

    toggles = Source(Toggles)            # a class, a function, a method, a module, or a `Path`
    draw_any(toggles.tree)               # its code as keys and values: a class's body, a function's
                                         # `parameters` and `locals`, a module's names
    toggles.tree['show_fps'] = False     # the file on disk says so from here on
    toggles.text                         # its source text; set it and the file is changed
    toggles.file                         # the whole file's `Source`: draw_editor(toggles.file, ...)

    Source(Toggles, at='launch')         # the code as it was when the app started
    Source(Toggles, at='HEAD~1')         # at a git commit

A scope is the code: there is nothing to load, save or keep in step. It is a light handle on
the one text of its file (`file`, model/source_file_model.py: the io that makes this fast), so a
class and the module that defines it are the same text and an edit through either shows in both.
A version that is not the file (`at`) is read only: a write to it is ignored.

What `where` takes: a module is its file, a class or a function the path of its qualified name in
that file's tree (`Knobs.scale`, a nested function under its parent's `locals`). A wrapped or
decorated function is the function it wraps (`__wrapped__`), a bound method its function, a
property its getter. Where a name is defined twice the last definition is meant. Something
without a Python source file (built in, typed at a prompt) raises TypeError.

Levers: `where` (what can be passed), `Scope.__init__` (`at`: the spelling of versions; the
versions themselves are model/source_version_model.py).
Not there: a definition under an `if` or `try` (its tree path holds the block, its qualified name
does not); the decorators above a definition are not part of its `text`.
Tests: tests/test_source_scope.py.
"""
import os
import types
from bisect import bisect_left

# Lever: how many `__wrapped__` are followed (a decorator that wraps itself would never end).
WRAPS = 32


def where(thing):
    """(file, key path) of the code of `thing`: the file it is written in and where it stands in
    that file's tree. To take another kind of object, unwrap it to a function or a class here."""
    from meltygui.core.runtime.paths import module_file
    from meltygui.model.tree_model import keys_of
    if isinstance(thing, os.PathLike):
        return os.path.abspath(thing), ()
    module = isinstance(thing, types.ModuleType)
    if not module and not isinstance(thing, type):
        for _ in range(WRAPS):
            inner = getattr(thing, '__wrapped__', None) or getattr(thing, '__func__', None) or getattr(thing, 'fget', None)
            if inner is None:
                break
            thing = inner
    path = module_file(thing)
    name = getattr(thing, '__qualname__', None)
    if path is None or not path.endswith(('.py', '.pyw')) or not (module or name):
        raise TypeError(f'Source({thing!r}): a class, a function or a module written in a Python file has code; this has none')
    return path, () if module else keys_of(name)


class Scope:
    __slots__ = ('path', 'keys', 'at', 'file')

    def __init__(self, thing, at=None):
        from meltygui.model.source_file_model import opened
        from meltygui.model.source_version_model import capture, version
        self.path, self.keys = where(thing)
        self.at = at
        capture(self.path)             # how its repository is now is the launch version (once, on a thread)
        self.file = opened(self.path) if at in (None, 'disk') else version(self.path, at)

    @property
    def tree(self):
        """The scope as keys and values (model/tree_model.py), read and written like a dict.
        The view that asks runs again when a reading lands: with other keys it is another object."""
        file = self.file
        file.tree
        grown = file.grown
        grown.note()
        return grown.under(self.keys)

    def lines(self):
        """(first, last) line of the scope in the text as last read; None when the code has it no longer."""
        file = self.file
        span = self.tree.span
        if span is None:
            return None
        defs = file.defs
        at = bisect_left(defs, span[0], key=lambda found: found[2])
        return (span[0], defs[at][3]) if at < len(defs) and defs[at][2] == span[0] else None

    @property
    def text(self):
        """The source of the scope: the whole file for a module, else the lines of the definition
        (as last read, like the tree). Set it and the file changes: what differs is spliced."""
        file = self.file
        if not self.keys:
            return file.text
        lines = self.lines()
        return '' if lines is None else file.native.span(*lines)

    @text.setter
    def text(self, text):
        file = self.file
        if file.fixed:
            return
        if not self.keys:
            file.text = text
            return
        self.tree
        if file.grown.version != file.head:
            file.grown.read()          # its place is of the text as it is now
        lines = self.lines()
        if lines is None:
            raise KeyError(f'{self!r} is not in the code any more')
        native = file.native
        low, high = native.start(lines[0]), native.start(lines[1] + 1)
        old = native.slice(low, high)
        # What both start and end with stays: one splice of what differs.
        head = next((at for at, (a, b) in enumerate(zip(old, text)) if a != b), min(len(old), len(text)))
        most = min(len(old), len(text)) - head
        tail = next((at for at in range(most) if old[-1 - at] != text[-1 - at]), most)
        if head + tail < max(len(old), len(text)):
            file.splice(low + head, high - tail, text[head:len(text) - tail])

    def __repr__(self):
        name = '.'.join(key for key in self.keys if key != 'locals') or 'the module'
        return f'<Source of {name} in {self.path}{f" at {self.at}" if self.at else ""}>'
