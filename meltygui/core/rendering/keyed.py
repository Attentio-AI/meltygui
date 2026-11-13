"""`Keyed`: how a value is read and written as keys and items. `draw_collection` (view/dict_view.py)
draws every value that has one, and `draw_any` sends such a value there when no view is nearer.

    keyed_as('Module', keys=lambda module: [name for name, _ in module.named_children()], get=getattr)

A `Keyed` is four functions of the value, nothing of the view:

    keys(value)             its keys in order: a list, a tuple or a range (indexed, `len`, `.index`)
    get(value, key)         the item; LookupError or AttributeError when the key is gone
    put(value, key, item)   write an edit into the value; None: its rows are read only
    count(value)            how many keys it has now, cheap: asked on idle runs to see a change

Which one a type gets (`keyed`, resolved once a type; the nearest class wins):

1. A `keyed_as` registration: a type, or the `__name__` of one this code should not import.
2. What the class says of itself: a dataclass and a named tuple are keyed by their fields.
3. The built in ones below: dict, list, tuple, deque, SimpleNamespace, DictConversion.
4. Anything with `keys`, `__getitem__` and `__len__`: a mapping, writable when it has `__setitem__`
   (MappingProxyType, UserDict, ChainMap, os.environ, an h5py group, a sqlite row, a DataFrame).

Not keyed, on purpose: any other object with a `__dict__` (a function, a module and a class have
one). To draw a class of yours as its attributes: `keyed_as(Mine, keys=ATTRS.keys, get=getattr, put=setattr)`.

Cost: a dict lookup by type a value. For a dict and a list every function is a C one.
"""
from operator import getitem, setitem

FOUND = {}         # type -> its Keyed or None: filled on demand, cleared by a registration
KEYED = {}         # type or type name -> Keyed: the registrations
DERIVED = [FOUND]  # the caches made from KEYED, cleared by a registration: the router adds its routes


class Keyed:
    __slots__ = ('keys', 'get', 'put', 'count')

    def __init__(self, keys, get=getitem, put=None, count=None):
        self.keys, self.get, self.put = keys, get, put
        self.count = count or (lambda value: len(keys(value)))


def keyed_as(*kinds, keys, get=getitem, put=None, count=None):
    """Read values of `kinds` (types, or type names) as keys and items. A later registration replaces an earlier one."""
    found = Keyed(keys, get, put, count)
    for kind in kinds:
        if not isinstance(kind, (type, str)):
            raise TypeError(f'keyed_as takes types and type names, not {kind!r}')
        KEYED[kind] = found
    for cache in DERIVED:
        cache.clear()
    return found


def attributes(value):
    """The public attributes of an object, without those its class hides with `@exclude`."""
    hidden = getattr(value.__class__, '__excluded_attrs__', ())
    return [name for name in vars(value) if name[0] != '_' and name not in hidden]


def fields(value):
    return list(value.__dataclass_fields__)


ITEMS = keyed_as(dict, keys=list, put=setitem, count=len)
MAPPING = Keyed(lambda value: list(value.keys()), put=setitem, count=len)
FROZEN_MAPPING = Keyed(MAPPING.keys, count=len)
SEQUENCE = keyed_as(list, 'deque', keys=lambda value: range(len(value)), put=setitem, count=len)
FROZEN_SEQUENCE = keyed_as(tuple, keys=SEQUENCE.keys, count=len)
ATTRS = keyed_as('SimpleNamespace', 'DictConversion', keys=attributes, get=getattr, put=setattr)
FIELDS = Keyed(fields, getattr, setattr)
FROZEN_FIELDS = Keyed(fields, getattr)
NAMED = Keyed(lambda value: value._fields, getattr)


def resolve(kind):
    for base in kind.__mro__[:-1]:
        found = KEYED.get(base) or KEYED.get(base.__name__)
        if found is not None:
            return found
        own = vars(base)
        if '__dataclass_fields__' in own:
            return FROZEN_FIELDS if base.__dataclass_params__.frozen else FIELDS
        if '_fields' in own and issubclass(base, tuple):
            return NAMED
    if all(hasattr(kind, name) for name in ('keys', '__getitem__', '__len__')):
        return MAPPING if hasattr(kind, '__setitem__') else FROZEN_MAPPING
    return None


def keyed(kind):
    """The `Keyed` of the type `kind`, or None when its values are not collections."""
    try:
        return FOUND[kind]
    except KeyError:
        found = FOUND[kind] = resolve(kind)
        return found
