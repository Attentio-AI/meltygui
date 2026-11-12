"""`draw_any`: draw a value with the `@gui` view registered for it. Apps import these names from `meltygui.rust`.

    @is_default_for(float)                                  # a type
    @is_default_for('tint', Shaped(tuple, (3,), float))     # a name, a shape
    @gui(height=22)
    def draw_tint(input_value: tuple, width=0., height=0.): ...

    changed, value = draw_any(value, name='gain', width=160)

`draw_any` is called like the view it picks: it takes that view's arguments
and returns its `(changed, value)`. A value that is read by keys (a dict, a
list, a dataclass: core/rendering/keyed.py) goes to `draw_collection`
(view/dict_view.py), which draws every item of it the same way.

What `@is_default_for` takes, and which registration wins (`default_for`,
most specific first):

1. A name (str): the name the value is drawn under, which is `draw_any`'s
   `name=` or the value's key in a dict.
2. A shape (`Shaped(of, shape, dtype)`, core/rendering/shaped.py): tuples of
   three floats, tensors of two dimensions. The most constrained one wins.
3. A type, or the `__name__` of one this code should not import ('Tensor'):
   the value's own type first, then its base classes in order.
4. `Keyed`: the view of every type that is read by keys (`keyed`) and has
   no class of its own registered.
5. `object`: the view of the rest.

A later registration of the same key replaces the earlier one, and an app
overrides a built in view by registering its own, before or after the built
in ones load: those (the modules in `BUILT_IN`) load when a value is first
routed and never replace an app's view. To change the order of the tiers,
edit `default_for`.

Cost: a name lookup and one dictionary lookup by the value's type. Types are
resolved once (`ROUTES`); shapes are only looked at for types that have a
shape registered.
"""
import importlib

from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.keyed import DERIVED, Keyed, keyed
from meltygui.core.rendering.shaped import Shaped, best_match

# The modules of the built in views: to add one, list its module here and register its views there.
BUILT_IN = ('meltygui.view.value_view', 'meltygui.view.dict_view')
NAMES = {}         # name -> view
SHAPES = {}        # Shaped -> view
TYPES = {}         # type -> view
# type -> (its view by type, the shape registrations that can fit it): filled on demand, cleared by a registration.
ROUTES = {}
DERIVED.append(ROUTES)        # (a `keyed_as` registration changes which types are collections)


def is_default_for(*keys):
    """Register the decorated view (made an `@gui` view if it is not one) as the default for `keys`:
    types, names and `Shaped` patterns, in any mix. Put it above `@gui`."""
    for key in keys:
        if not isinstance(key, (type, str, Shaped)):
            raise TypeError(f'is_default_for takes types, names and Shaped patterns, not {key!r}')

    def register(view):
        if not getattr(view, '__gui__', False):
            view = gui(view)
        for key in keys:
            table = SHAPES if isinstance(key, Shaped) else NAMES if isinstance(key, str) else TYPES
            # (A built in view never takes the place of an app's, whichever was registered first.)
            if view.__module__ not in BUILT_IN or getattr(table.get(key), '__module__', BUILT_IN[0]) in BUILT_IN:
                table[key] = view
        ROUTES.clear()
        return view
    return register


def route(kind):
    """What `ROUTES` keeps for the type `kind`: the view of its nearest registered class (by type, then
    by class name) and the shape registrations whose type it is."""
    for module in BUILT_IN:
        importlib.import_module(module)
    view = None
    for base in kind.__mro__[:-1]:
        view = TYPES.get(base) or NAMES.get(base.__name__)
        if view is not None:
            break
    else:
        view = keyed(kind) and TYPES.get(Keyed) or TYPES.get(object)
    found = ROUTES[kind] = (view, tuple(item for item in SHAPES.items() if item[0].matches_type(kind) >= 0))
    return found


def default_for(value, name=None):
    """The view registered for `value` when it is drawn under `name`."""
    if name.__class__ is str and name in NAMES:
        return NAMES[name]
    kind = value.__class__
    view, shapes = ROUTES.get(kind) or route(kind)
    if shapes:
        return best_match(shapes, value, kind) or view
    return view


def draw_any(input_value=None, **arguments):
    """Draw `input_value` with its default view. The arguments and the returned `(changed, value)` are that view's."""
    return default_for(input_value, arguments.get('name'))(input_value, **arguments)
