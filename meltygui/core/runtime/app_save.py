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
                           Each value that changed is written over its default, as a literal,
                           where it stands: the layout and the comments around it stay.

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
(tree_model.py, native/source/src/tree.rs). Nothing in Python looks for the place.

The layout of a field is kept: only the smallest places that changed are written (`places`), a
number in a dict, an argument of a constructor, a list whose items changed. New code gets the
default layout of `wrap` (tree_model.py; its levers: `WIDTH`, `TAB`): a list or dict written
anew that does not fit where it stands has the whole field laid out by it. Before the
file is replaced `compile` judges the new text (the judge of syntax everywhere:
native/source/README.md) and the tree is read again to see that each value landed. Nothing is
read or parsed at launch, and nothing at exit when no field changed. A field is written when
- the class body assigns it once, to one name, at the top of the class (not under an `if`),
- the file still holds the value this run started from (its text is evaluated where the class
  stands, `given`): a default edited while the app ran is left as it is, and said so on stderr. A hotswap that runs the class again makes its new
  defaults the start (`Coded.again`),
- its value is a literal (`spelled`). An `Enum` member is written by name (`Mode.FAST`), when
  that name gives the member where the class stands: an Enum the file reaches as `modes.Mode` is
  left and said.

A dataclass object is written as its constructor call, `Point(x=1, y=2)`; where the file has
the call already its arguments are written in place, by place or by name as they stand
(`inline`).

An expression in the file (`2 / 3`, `Mode.FAST` inside a dict) is no reason to lose the field.
It stays in the code while its own value is unchanged and is replaced by the literal, said on
stderr, once it changed (`kept`): a view that edits `Mode.FAST` as the int it is leaves an int.

Under `meltygui.embedded_imports()` (GUI Gym showing an app's views) `@save` does nothing: no
file of the app is read or written.

Levers: `named` (where a pickle lives and what it is called), `fields` (what counts as a field),
`spelled` (the types a to_code field may hold), `places` (how much of a layout a write keeps),
`kept` (which expressions outlive a place written anew).
Not there: saving one class in the middle of a run without the rest (`checkpoint()` saves all).
Tests: tests/test_app_save.py.
"""
import enum
import math
import os
import sys
import time

from meltygui.core.runtime.app import _embedded_app_state
from meltygui.core.runtime.paths import module_file

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
    if kind is Kept:
        return str(value)
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
    if isinstance(value, enum.Enum) and value.name in kind.__members__:
        return f'{kind.__qualname__}.{value.name}'      # (`write` sees that the file knows that name)
    if hasattr(kind, '__dataclass_fields__'):
        items = ', '.join(f'{name}={spelled(getattr(value, name))}' for name in built(kind))
        return f'{kind.__qualname__}({items})'
    raise TypeError(f'{value!r} has no literal: a to_code field holds numbers, texts, True, False, None, '
                    f'Enum members, dataclass objects and lists, tuples, dicts and sets of those')


def built(kind):
    """The names of the fields the constructor of the dataclass `kind` takes, in its order."""
    from dataclasses import fields
    return [field.name for field in fields(kind) if field.init]


class Kept(str):
    """An expression of the file that is written back as it stands: `Mode.FAST`."""
    __slots__ = ()


def given(text, owner):
    """The value the source `text` gives in the body of the class `owner`."""
    return eval(text, vars(sys.modules[owner.__module__]), dict(vars(owner)))


def places(value, branch, key, start, lost, out):
    """Collect in `out` the smallest places of the file that differ from `value`, as (branch, key,
    what to write there): `start` is what the file gives under `key` of `branch`. A number in a
    dict is one place and nothing around it moves; a list or dict whose keys changed is one
    place, written anew (`write` lays the whole field out by `wrap` when that does not fit its
    line). To keep more of a layout through a write, add a row that goes on down."""
    if spelled(value) == spelled(start):
        return
    found = branch[key]
    kind = getattr(found, 'kind', None)
    if kind == 'dict' and type(value) is dict and list(value) == list(start) == list(found):
        for name, item in value.items():
            places(item, found, name, start[name], lost, out)
    elif kind in ('list', 'tuple') and type(value).__name__ == kind and len(value) == len(start) == len(found):
        for at, item in enumerate(value):
            places(item, found, at, start[at], lost, out)
    elif kind == 'call' and (names := arguments(value, found, start)) and not defaults(value, start, names):
        for at, name in names.items():
            places(getattr(value, name), found, at, getattr(start, name), lost, out)
    else:
        out.append((branch, key, kept(value, found, start, lost)))


def kept(value, found, start, lost):
    """`value` with the file's expressions (`found`, the tree's entry) back where what they give
    (`start`) is still the value, as `Kept`. One whose value changed is put in `lost`: its literal
    takes its place. To keep more of the code through a write (a set's items), add its row here."""
    from meltygui.model.tree_model import Code
    kind = getattr(found, 'kind', None)
    if kind == 'call' or isinstance(found, Code):
        text = found.text if kind else str(found)
        if spelled(value) == spelled(start):
            return Kept(text)
        call = inline(value, found, start, lost) if kind else None
        if call is not None:
            return call
        if not isinstance(value, enum.Enum) and type(start) is not bytes:
            lost.append(text)      # (a member written over a member is no loss; the tree reads b'..' as code)
    elif kind is None:
        pass                       # a literal
    elif kind == 'dict' and type(value) is dict and list(start) == list(found):
        # (A key or an item that is new has no place in the file: it is written as it is.)
        return {key: kept(item, found[key], start[key], lost) if key in start else item for key, item in value.items()}
    elif kind in ('list', 'tuple') and type(value).__name__ == kind and len(start) == len(found):
        return type(value)(kept(item, found[at], start[at], lost) if at < len(start) else item
                           for at, item in enumerate(value))
    else:
        try:
            spelled(plain(found))
        except TypeError:
            lost.append(found.text)    # it holds expressions and is another kind of value now
    return value


def inline(value, found, start, lost):
    """The constructor call `found` of a dataclass object with the arguments `value` has now, each
    where and how the call gives it (by place or by name); a field the call left to its default
    is added by name once it changed. None when the call is no plain constructor of it (`*args`,
    another class): the object is then written whole, `Class(name=value, ...)`."""
    names = arguments(value, found, start)
    if names is None:
        return None
    items = [f'{key}=' * isinstance(key, str) + spelled(kept(getattr(value, name), found[key], getattr(start, name), lost))
             for key, name in names.items()]
    items += [f'{name}={spelled(getattr(value, name))}' for name in defaults(value, start, names)]
    return Kept(f"{found.text.partition('(')[0]}({', '.join(items)})")


def arguments(value, found, start):
    """{argument of the call `found` (its place, or its name): the field it gives} when the call is
    a plain constructor of the dataclass both `value` and `start` are; else None."""
    kind = type(value)
    if type(start) is not kind or not hasattr(kind, '__dataclass_fields__'):
        return None
    names, takes = {}, built(kind)
    for key, item in found.items():
        name = key if isinstance(key, str) else takes[key] if key < len(takes) else None
        if name not in takes or str(item).startswith('*'):
            return None
        names[key] = name
    return names


def defaults(value, start, names):
    """The fields a constructor call left to their defaults (`names`: those it gives) that changed."""
    return [name for name in built(type(value)) if name not in names.values()
            and spelled(getattr(value, name)) != spelled(getattr(start, name))]


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
        field that was left as it is and each expression a literal replaced; raises when the file
        could not be written."""
        changed = {}               # label -> its literal on one line: what is compared, never the layout
        values = {}                # label -> (the class it is a field of, its value)
        notes = []
        for owner, name, label in self.fields:
            try:
                text = spelled(getattr(owner, name))
            except TypeError as error:
                notes.append(f'{label}: {error}')
                continue
            if text != self.held[label]:
                changed[label], values[label] = text, (owner, getattr(owner, name))
        if not changed:
            return notes
        from meltygui.model.source_model import Source
        from meltygui.model.tree_model import Code, keys_of, quote, wrap
        with open(self.path, 'rb') as file:
            data = file.read()
        source = Source(data.decode('utf-8'), self.path)
        try:
            top = source.tree
            tree = source.grown
            if tree.version != source.head:
                tree.read()        # a long text is read on its thread: have it here, now
                top = source.tree
            wrote = {}                 # label -> [(branch, key, the text written there)]
            for label, text in changed.items():
                # (Where a class stands in its file's tree is the lookup `Source(cls)` uses: tree_model.py, `keys_of`.)
                *path, name = keys_of(f'{self.cls.__qualname__}.{label}')
                body = top.down(path)
                if body is None or name not in body or f'{name}#1' in body:
                    notes.append(f'{label} needs one plain assignment at the top of its class')
                    continue
                now = read(body[name])
                owner, value = values[label]
                try:
                    if spelled(given(text, owner)) != text:     # an Enum member is written by name: the file has to know it
                        raise ValueError('it reads back as another value')
                except Exception as error:
                    notes.append(f'{label}: `{text}` is not what the file would read back '
                                 f'({type(error).__name__}: {error}): left as it is')
                    continue
                try:
                    start, lost, spots = given(now, owner), [], []
                    said = spelled(start)
                    if said == self.held[label]:
                        places(value, body, name, start, lost, spots)
                except Exception:
                    said = None
                if said == text:
                    self.held[label] = text         # the file says so already (an edit a hotswap brought in)
                    continue
                if said != self.held[label]:
                    notes.append(f'{label} is `{now}` in the file, not the `{self.held[label]}` this run '
                                 f'started from: left as it is')
                    continue
                if any(type(item) in (list, tuple, dict, set) and '\n' in wrap(item, *tree.margin(branch, key), spelled)
                       for branch, key, item in spots):
                    # What grew does not fit where it stands: the whole field is laid out anew.
                    lost.clear()
                    spots = [(body, name, kept(value, body[name], start, lost))]
                notes += [f'{label}: `{code}` is written as its value, it changed' for code in lost]
                wrote[label] = []
                for branch, key, item in spots:
                    # One splice a place, at the span the tree found; a list or dict is wrapped to the line it starts on.
                    indent, column = tree.margin(branch, key)
                    _, low, high = tree.spot(branch, key)
                    laid = quote(item, source.native.slice(low, high)) if type(item) is str else wrap(item, indent, column, spelled)
                    branch[key] = Code(laid)
                    wrote[label].append((branch, key, laid))
            if wrote:
                text = source.text
                compile(text, self.path, 'exec')
                source.settle()                     # the readings the splices asked for end before this one
                tree.read()
                for label, spots in wrote.items():
                    for branch, key, laid in spots:
                        if key not in branch or read(branch[key]) != laid:
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
