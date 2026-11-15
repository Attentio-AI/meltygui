"""`@save`: a class whose attributes the app keeps between runs.

    from meltygui import os_window, save

    @save
    class Tiles:
        tree = {'id': 0, 'axis': 'x', 'cells': [{'id': 1}, {'id': 2}]}

    @save(to_code=True)
    class Look:
        font_size = 21

    @os_window(name='Tile View', app_id='tile-view')
    def main(width=None, height=None):
        draw_tiles(Tiles.tree, name='tiles', width=width, height=height)

The class is the state: there is no instance. Its public plain attributes are the fields and the
values its body gives them are the defaults. A name that starts with `_`, a method and a property
are not saved. A class in its body is saved with it (`Look.Editor.tab_width`).

    @save                  a pickle of its own beside the module that defines the class, named
                           after the class: `Tiles.pkl` next to main.py. One store, one file
                           (the locality rule, docs/APPS.md). {field: value}, plain `pickle`.
    @save('layout.pkl')    that file instead: beside the module, or where an absolute path says.
    @save(to_code=True)    the class's own source. Nothing is read: the code is the saved state.
                           Each value that changed is written over its default, as a literal;
                           one too long for its line has its items on indented lines below.

A pickle is read where the class is decorated, so the values are there for the lines below it. It
costs one small file read during the import (0.03 ms for tile-view's layout, and 0.6 ms for the
process's first `import pickle`: docs/GUI_REQUIREMENTS.md, section 6); what its values are
instances of has to be defined above the class. A field the class has no longer
is dropped, a new one keeps its default. A file that does not load is moved aside
(`Tiles.pkl.broken-<time>`) and said on stderr: the app starts from its defaults and the next
save does not overwrite the evidence.

Everything is written by `write`, which the app calls at exit and at `meltygui.checkpoint()`
(app.py), never after a failed frame. A write is whole or not at all: a failure leaves the last
good file and is reported. A pickle whose bytes are what the file holds is not written again.

Writing to code goes through the Rust `Source` (model/source_model.py): `source.tree` finds the
class and the assignment, and an assignment to it splices the new literal over the old one
(tree_model.py, native/source/src/tree.rs), laid out by `wrap` there (its levers: `WIDTH`, `TAB`). Nothing in Python looks for the place. Before the
file is replaced `compile` judges the new text (the judge of syntax everywhere:
native/source/README.md) and the tree is read again to see that each value landed. Nothing is
read or parsed at launch, and nothing at exit when no field changed. A field is written when
- the class body assigns it once, to one name, at the top of the class (not under an `if`),
- the file still holds the value this run started from: an expression (`2 / 3`) and a default
  edited while the app ran are left as they are, and said so on stderr. A hotswap that runs the
  class again makes its new defaults the start (`Coded.again`),
- its value is a literal (`spelled`).

Under `meltygui.embedded_imports()` (GUI Gym showing an app's views) `@save` does nothing: no
file of the app is read or written.

Levers: `named` (where a pickle lives and what it is called), `fields` (what counts as a field),
`spelled` (the types a to_code field may hold).
Not there: saving one class in the middle of a run without the rest (`checkpoint()` saves all).
Tests: tests/test_app_save.py.
"""
import math
import os
import sys
import time

from meltygui.core.runtime.app import _embedded_app_state

_kept = {}            # 'module.Class' -> Pickled or Coded, in decoration order


def save(file=None, *, to_code=False):
    """`@save`, `@save('file.pkl')` or `@save(to_code=True)` above a class: keep its attributes
    between runs, in a pickle beside its module or in its own source."""
    if isinstance(file, type):
        return keep(file, None, to_code)
    return lambda cls: keep(cls, file, to_code)


def keep(cls, file, to_code):
    if not isinstance(cls, type):
        raise TypeError('@save decorates a class: its attributes are the saved fields')
    if to_code and file is not None:
        raise TypeError('@save(to_code=True) writes into the class\'s own file, not into a pickle')
    if _embedded_app_state.get() is not None:
        return cls                 # a host that only shows the app's views: the defaults, nothing read or written
    key = f'{cls.__module__}.{cls.__qualname__}'
    kept = _kept.get(key)
    if kept is not None:
        kept.again(cls)            # a hotswap ran the class body again: it patches the first class, which stays the live one
        return cls
    if to_code:
        _kept[key] = Coded(cls)
    else:
        path = named(cls, file)
        if any(other.path == path for other in _kept.values()):
            raise ValueError(f'Another class is saved in {path}: give {cls.__qualname__} a file of its own, @save("name.pkl")')
        _kept[key] = Pickled(cls, path)
    return cls


def module_file(cls):
    """The file of the module that defines `cls`, or None when it has none (typed at a prompt)."""
    path = getattr(sys.modules.get(cls.__module__), '__file__', None)
    return os.path.abspath(path) if path else None


def named(cls, file=None):
    """The pickle of `cls`: `file`, or `<Class>.pkl`, beside the module that defines the class.
    To keep pickles somewhere else, change it here."""
    file = os.fspath(file) if file is not None else f'{cls.__name__}.pkl'
    if os.path.isabs(file):
        return os.path.normpath(file)
    module = module_file(cls)
    if module is None:
        raise TypeError(f'@save keeps {cls.__qualname__} beside its module, and it has no file: give an absolute path')
    return os.path.normpath(os.path.join(os.path.dirname(module), file))


def fields(cls, label=''):
    """(owner, name, label) of every field of `cls` and of the classes in its body."""
    for name, value in vars(cls).items():
        if name.startswith('_'):
            continue
        if isinstance(value, type):
            if value.__qualname__ == f'{cls.__qualname__}.{name}':
                yield from fields(value, f'{label}{name}.')
        elif not callable(value) and not hasattr(value, '__get__'):
            yield cls, name, label + name


class Pickled:
    """A class kept in a pickle of its own, read when it is made."""
    __slots__ = ('cls', 'fields', 'path', 'wrote')

    def __init__(self, cls, path):
        self.cls = cls
        self.fields = list(fields(cls))
        self.path = path
        self.wrote = None          # the bytes the file holds, as read or as last written
        self.load()

    def load(self):
        """Put the file's values on the class. Only fields the class still has land."""
        try:
            with open(self.path, 'rb') as file:
                data = file.read()
        except FileNotFoundError:
            return                 # a first launch: the defaults
        except OSError as error:
            print(f'meltygui: @save {self.path} could not be read ({type(error).__name__}: {error})', file=sys.stderr)
            return
        import pickle
        try:
            values = pickle.loads(data)
            if not isinstance(values, dict):
                raise ValueError(f'it holds a {type(values).__name__}, not the fields of a class')
        except Exception as error:
            broken = f'{self.path}.broken-{time.strftime("%Y%m%d-%H%M%S")}'
            try:
                os.replace(self.path, broken)
            except OSError:
                broken = None
            print(f'meltygui: @save {self.path} did not load ({type(error).__name__}: {error})'
                  + (f'; moved to {broken}' if broken else ''), file=sys.stderr)
            return
        self.wrote = data
        for owner, name, label in self.fields:
            if label in values:
                setattr(owner, name, values[label])

    def again(self, cls):
        """The class body ran again (a hotswap): the live class keeps what it holds."""

    def write(self):
        import pickle
        values = {label: getattr(owner, name) for owner, name, label in self.fields}
        data = pickle.dumps(values, protocol=pickle.HIGHEST_PROTOCOL)
        if data != self.wrote:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            replace(self.path, data)
            self.wrote = data
        return []


def spelled(value):
    """The literal of `value` on one line, as Python prints it, with a set's items sorted so their order never
    counts. TypeError for a value that has no literal: to let a to_code field hold another type,
    add its row here."""
    kind = type(value)
    if kind in (list, tuple, set):
        items = [spelled(item) for item in value]
        if kind is list:
            return f"[{', '.join(items)}]"
        if kind is tuple:
            return f'({items[0]},)' if len(items) == 1 else f"({', '.join(items)})"
        return '{' + ', '.join(sorted(items)) + '}' if items else 'set()'
    if kind is dict:
        return '{' + ', '.join(f'{spelled(key)}: {spelled(item)}' for key, item in value.items()) + '}'
    if kind in (str, bytes, int, bool, type(None)) or (kind is float and math.isfinite(value)):
        return repr(value)
    raise TypeError(f'{value!r} has no literal: a to_code field holds numbers, texts, True, False, None '
                    f'and lists, tuples, dicts and sets of those')


class Coded:
    """A class kept in its own source: its file, and the literal each field had when the file
    last agreed with it (at launch, and after every write)."""
    __slots__ = ('cls', 'fields', 'path', 'held')

    def __init__(self, cls):
        self.cls = cls
        self.fields = list(fields(cls))
        self.path = module_file(cls)
        if self.path is None or '<locals>' in cls.__qualname__:
            raise TypeError(f'@save(to_code=True) needs a class at the top of a file: {cls.__qualname__}')
        self.held = {label: spelled(getattr(owner, name)) for owner, name, label in self.fields}

    def again(self, cls):
        """The class body ran again from the file as it is now: its defaults are what the file holds."""
        for owner, name, label in fields(cls):
            if label in self.held:
                try:
                    self.held[label] = spelled(getattr(owner, name))
                except TypeError:
                    pass           # no literal: `write` says so when the field changes

    def write(self):
        """Write the fields that changed over their defaults in the file. Returns a note for each
        field that was left as it is; raises when the file could not be written."""
        changed = {}               # label -> its literal on one line: what is compared, never the layout
        values = {}
        notes = []
        for owner, name, label in self.fields:
            try:
                text = spelled(getattr(owner, name))
            except TypeError as error:
                notes.append(f'{label}: {error}')
                continue
            if text != self.held[label]:
                changed[label], values[label] = text, getattr(owner, name)
        if not changed:
            return notes
        from meltygui.model.source_model import Source
        from meltygui.model.tree_model import Branch, Code, wrap
        with open(self.path, 'rb') as file:
            data = file.read()
        source = Source(data.decode('utf-8'), self.path)
        try:
            top = source.tree
            tree = source.grown
            if tree.version != source.head:
                tree.read()        # a long text is read on its thread: have it here, now
                top = source.tree
            wrote = {}
            for label, text in changed.items():
                *path, name = f'{self.cls.__qualname__}.{label}'.split('.')
                body = top
                for part in path:
                    body = body.get(part) if isinstance(body, Branch) else None
                if not isinstance(body, Branch) or name not in body or f'{name}#1' in body:
                    notes.append(f'{label} needs one plain assignment at the top of its class')
                    continue
                now = read(body[name])
                if now == text:
                    self.held[label] = text         # the file says so already (an edit a hotswap brought in)
                elif now != self.held[label]:
                    notes.append(f'{label} is `{now}` in the file, not the `{self.held[label]}` this run '
                                 f'started from: left as it is')
                else:
                    # One splice, at the span the tree found, wrapped to the line it starts on.
                    laid = wrap(values[label], *tree.margin(body, name), spelled)
                    body[name] = Code(laid)
                    wrote[label] = (body, name, laid)
            if wrote:
                text = source.text
                compile(text, self.path, 'exec')
                source.settle()                     # the readings the splices asked for end before this one
                tree.read()
                for label, (body, name, laid) in wrote.items():
                    if name not in body or read(body[name]) != laid:
                        raise ValueError(f'{label} did not read back as written')
                replace(self.path, text.encode('utf-8'), data)
                stale(self.path)
                self.held.update({label: changed[label] for label in wrote})
            return notes
        finally:
            source.close()


def read(found):
    """What the tree holds under a key, spelled like `spelled`; an expression is its source text."""
    from meltygui.model.tree_model import Code
    if isinstance(found, Code):
        return str(found)
    try:
        return spelled(plain(found))
    except TypeError:
        return found.text          # a list or dict that holds an expression, a call


def plain(found):
    """The Python value of a tree entry: a `Branch` (tree_model.py) is a list, tuple, set or dict."""
    kind = getattr(found, 'kind', None)
    if kind is None:
        return found
    if kind == 'dict':
        return {key: plain(item) for key, item in found.items()}
    if kind in ('list', 'tuple', 'set'):
        return {'list': list, 'tuple': tuple, 'set': set}[kind](plain(item) for item in found.values())
    raise TypeError(found.text)


def replace(path, new, old=None):
    """Put the bytes `new` in place of the file, whole or not at all. `old`: what a file that is
    there must still hold (a source someone may be editing); it keeps its mode."""
    temporary = f'{path}.{os.getpid()}.tmp'
    try:
        with open(temporary, 'wb') as file:
            file.write(new)
        if old is not None:
            if not os.access(path, os.W_OK):
                raise PermissionError('the file is read-only')
            os.chmod(temporary, os.stat(path).st_mode & 0o7777)
            with open(path, 'rb') as file:
                if file.read() != old:
                    raise ValueError('the file changed while it was being saved')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def stale(path):
    """Drop the compiled copies of a source that was written: a copy is told from a newer source
    by its second and its size, and both can be the same."""
    from importlib.util import cache_from_source
    for optimization in ('', 1, 2):
        compiled = cache_from_source(path, optimization=optimization)
        if os.path.exists(compiled):
            os.remove(compiled)


def write():
    """Save every class: its pickle, or its changes into its source. What the app calls at exit
    and at `checkpoint()` (app.py). Reported, never raised: it runs on the exit path. Returns
    False when a file could not be written; a to_code field left as it is by the rules above is
    only said."""
    written = True
    for key, kept in _kept.items():
        try:
            notes = kept.write()
        except Exception as error:
            written = False
            notes = [f'not written ({type(error).__name__}: {error})']
        for note in notes:
            print(f'meltygui: @save {key} in {kept.path}: {note}', file=sys.stderr)
    return written
