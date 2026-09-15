import inspect


class _MockNode:
    """A lazy reference to a location reachable from the mock meltygui, e.g.
    ``meltygui.type_defaults[cls]`` or ``meltygui.cache.invalidate_up_by_obj``.

    Attribute/item access returns further nodes so chains keep building; writes
    (``__setattr__`` / ``__setitem__``) and calls (``__call__``) append an entry
    to the shared op-log instead of touching anything real. ``path`` is the chain
    of steps needed to reach this location from the meltygui root.
    """

    def __init__(self, ops, path):
        object.__setattr__(self, "_ops", ops)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _MockNode(self._ops, self._path + (("attr", name),))

    def __setattr__(self, name, value):
        self._ops.append((self._path + (("attr", name),), "set", value))

    def __getitem__(self, key):
        return _MockNode(self._ops, self._path + (("item", key),))

    def __setitem__(self, key, value):
        self._ops.append((self._path + (("item", key),), "set", value))

    # Without these, ``x in node`` falls back to the sequence protocol and calls
    # __getitem__(0), __getitem__(1), ... forever (each returns a node, never
    # IndexError) -> hang. A pre-init collection is empty: nothing is in it.
    def __contains__(self, key):
        return False

    def __iter__(self):
        return iter(())

    def __call__(self, *args, **kwargs):
        self._ops.append((self._path, "call", (args, kwargs)))
        # Allow chaining off the return value (e.g. meltygui.foo(...).bar = x). The
        # call's args travel with the step so replay can reproduce it faithfully.
        return _MockNode(self._ops, self._path + (("call", args, kwargs),))


class MockMelty:
    """Stand-in for ``Melty`` used before ``Melty.init()`` runs.

    ``core_decoration.py`` must not import ``meltygui`` (circular import), so all
    pre-init access to Melty — chiefly the ``@defaults`` decorator writing
    ``type_defaults[cls] = meta`` at class-definition time — goes through this
    recorder. Every write and call is appended to a flat op-log; ``replay``
    re-applies that log against the real Melty during init so nothing done at
    import/decoration time is lost.
    """
    def __init__(self):
        # Each entry is (path, kind, payload):
        #   path    - tuple of ("attr", name) / ("item", key) / ("call",) steps
        #   kind    - "set" | "call"
        #   payload - the value (for "set") or (args, kwargs) (for "call")
        object.__setattr__(self, "_ops", [])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _MockNode(self._ops, (("attr", name),))

    def __setattr__(self, name, value):
        self._ops.append(((("attr", name),), "set", value))

    @staticmethod
    def _walk(target, path):
        cur = target
        for kind, *rest in path:
            if kind == "attr":
                cur = getattr(cur, rest[0])
            elif kind == "item":
                cur = cur[rest[0]]
            elif kind == "call":
                args, kwargs = rest
                cur = cur(*args, **kwargs)
        return cur

    def _apply(self, target, path, kind, payload):
        if kind == "set":
            container = self._walk(target, path[:-1])
            last_kind, last_arg = path[-1]
            if last_kind == "attr":
                setattr(container, last_arg, payload)
            else:  # item
                container[last_arg] = payload
        elif kind == "call":
            fn = self._walk(target, path)
            args, kwargs = payload
            fn(*args, **kwargs)

    def replay(self, target):
        """Re-apply every recorded interaction against the real meltygui ``target``.

        Best-effort: the only op that must land is ``@defaults`` writing
        ``type_defaults[cls] = meta``. Other entries are incidental side effects
        of pre-init read-chains (e.g. ``type_defaults.get(...)`` lookups or
        live-attribute registration on objects built before init) — those re-run
        naturally at runtime, so a failure to replay one is skipped, not fatal.
        """
        applied = skipped = 0
        for path, kind, payload in self._ops:
            try:
                self._apply(target, path, kind, payload)
                applied += 1
            except Exception as exc:
                skipped += 1
                names = ".".join(str(s[1]) for s in path if len(s) > 1)
                print(f"MockMelty.replay: skipped {kind} on '{names}': {exc!r}")
        return applied, skipped


class Core:
    melty = MockMelty()


def defaults(*args, **kwargs):
    """Class decorator setting Meta defaults.

    Without ``attr`` the Meta applies to the class itself (registered in
    ``type_defaults``). With ``attr`` the remaining kwargs become the Meta for
    that attribute, stored as ``{name}_meta`` on the class — exactly where
    ``Meta.get_child_meta`` looks it up. ``attr`` accepts a single name or an
    iterable of names sharing the same overrides:

        @defaults(attr="some_attrib", show_tint=False, show_bg=True)
        @defaults(show_name=True)            # class-level default
        class Foo: ...

    ``attrib`` is accepted as an alias for ``attr``.
    """
    attr = kwargs.pop("attr", None)
    if attr is None:
        attr = kwargs.pop("attrib", None)
    else:
        kwargs.pop("attrib", None)

    def decorator(cls):

        # from meltygui.views.core_meta import Meta
        if attr is None:
            # DecorationManager.melty.type_defaults[cls] = Meta(**kwargs)
            for key in kwargs:
                Core.melty.default_kwargs_by_type[cls][key] = kwargs[key]

        else:
            names = [attr] if isinstance(attr, str) else list(attr)
            for name in names:
                for key in kwargs:
                    Core.melty.default_kwargs_by_attrib_type[cls][name][key] = kwargs[key]
                    print(f"Registered default for {cls.__name__}.{name}: {key}={kwargs[key]}")
                    if key == "view_function" or key == "func":
                        Core.melty.default_funcs_by_name_type[cls][name] = kwargs[key]

                # child_meta = Meta(**kwargs)
                # child_meta.name = name
                # setattr(cls, f"{name}_meta", child_meta)
        return cls

    return decorator


def attribute(func):
    """Decorator that makes a method appear in __dict__"""
    func._add_to_dict = True
    return func

class auto_eval:
    _add_to_dict = True  # Class attribute

    def __init__(self, fget=None, fset=None, fdel=None):
        self.fget = fget
        self._add_to_dict = True  # Set on the instance
        self.fset = fset
        self.fdel = fdel
        self.name = None
        self.last_known_value = None


    def __set_name__(self, owner, name):
        self.name = name
        self.private_name = f'_{name}'

    def __get__(self, obj, objtype=None):
        if obj is None:
            self.last_known_value = None
            return self

        # Register on first access if not already registered
        if obj not in Core.melty.live_attributes:
            Core.melty.live_attributes[obj] = set()
        if self.name not in Core.melty.live_attributes[obj]:
            Core.melty.live_attributes[obj].add(self.name)

        if self.fget is None:
            new_val = obj.__dict__.get(self.private_name)
            self._on_change(obj, self.last_known_value, new_val)
            self.last_known_value = new_val
            return new_val

        new_val = self.fget(obj)
        self._on_change(obj, self.last_known_value, new_val)
        self.last_known_value = new_val
        return new_val

    def __set__(self, obj, value):
        # Register on first set if not already registered
        if obj not in Core.melty.live_attributes:
            Core.melty.live_attributes[obj] = set()
        if self.name not in Core.melty.live_attributes[obj]:
            Core.melty.live_attributes[obj].add(self.name)

        old_value = obj.__dict__.get(self.private_name)

        if self.fset is None:
            obj.__dict__[self.private_name] = value
        else:
            self.fset(obj, value)

        # Your custom callback logic here
        if old_value != value:
            self._on_change(obj, old_value, value)

    def __delete__(self, obj):
        if self.fdel is None:
            del obj.__dict__[self.private_name]
        else:
            self.fdel(obj)

    def setter(self, fset):
        return type(self)(self.fget, fset, self.fdel)

    def deleter(self, fdel):
        return type(self)(self.fget, self.fset, fdel)

    def _on_change(self, obj, old_value, new_value):
        from meltygui.utils.glfw_utils import request_render
        deep_refresh_names = getattr(self, '__deep_refresh__', set())

        if Core.melty.silence_invalidate:
            return

        excluded = getattr(obj, '__excluded_attrs__', set())
        invalidate_all_flag = getattr(self, '__invalidate_all__', set())

        do_deep_refresh = self.name in deep_refresh_names and not Core.melty.window_drag
        visible = self.name not in excluded
        visible = visible or do_deep_refresh

        if self.name in invalidate_all_flag:
            Core.melty.cache.invalidate_all()
            print(f"Invalidate all called due to change in {self.name}")
            request_render()
            return

        if old_value != new_value:
            if visible and not self.name.startswith('_') \
                    and self.name != "driver" and Core.melty.frame_count > 3:
                Core.melty.last_attr = self.name
                from meltygui.debug.invalidation_tracker import Note
                if do_deep_refresh:
                    note = Note(name="Core decoration", reason="invalidate_up_by_obj", tint=(0, 0, 1))
                    Core.melty.cache.invalidate_up_by_obj(obj=obj, name=self.name, max_depth=6, force=True, note=note)
                    request_render()

                    if hasattr(self, "context_menu_ds"):
                        print(f"Context menu ds found, invalidating {self.name}")
                else:
                    note = Note(name="Core decoration", reason="invalidate_up_by_obj", tint=(0, 0, 1))

                    Core.melty.cache.invalidate_up_by_obj(obj, self.name, max_depth=3, note=note)
                    request_render()



        """Override this or add your universal callback logic here"""


    def setter(self, fset):
        return type(self)(self.fget, fset, self.fdel)

    def deleter(self, fdel):
        return type(self)(self.fget, self.fset, fdel)


def deep_refresh(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__deep_refresh__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__deep_refresh__', merged_names)
        return cls

    return decorator
#
# def tint(*args, **kwargs):
#     def decorator(cls):
#         if len(args) == 1 and isinstance(args[0], (list, set, tuple, dict)):
#             from_args = args[0]
#             setattr(cls, '__tint__', from_args)
#         return cls
#
#     return decorator

def invalidate_all(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__invalidate_all__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__invalidate_all__', merged_names)
        return cls

    return decorator

global_hotkeys = {}

def no_save(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__no_save__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__no_save__', merged_names)
        return cls

    return decorator


def exclude(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__excluded_attrs__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__excluded_attrs__', merged_names)
        return cls

    return decorator


def no_save_exclude(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__excluded_attrs__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)
        setattr(cls, '__excluded_attrs__', merged_names)

        already_no_save = getattr(cls, '__no_save__', set())
        merged_names_ns = already_no_save.union(set(from_args))
        merged_names_ns = merged_names_ns.union(from_args)

        setattr(cls, '__no_save__', merged_names_ns)

        return cls

    return decorator



def hotkey(key):
    """
    This is the decorator factory. It takes arguments for the decorator.
    """

    def actual_decorator(func):
        """
        This is the actual decorator. It takes the function to be decorated.
        """
        sig = inspect.signature(func)
        params = sig.parameters

        if isinstance(key, int):
            from meltygui.state.draw_state import Hotkey
            the_hotkey = Hotkey(key=key)
        else:
            the_hotkey = key


        def wrapper(*args, **kwargs):
            to_remove = []
            for name, arg in kwargs.items():
                if name not in params:
                    to_remove.append(name)
            for name in to_remove:
                kwargs.pop(name)

            for wanted_name, param in params.items():
                if wanted_name in Core.melty.global_attrs and wanted_name not in kwargs:
                    kwargs[wanted_name] = Core.melty.global_attrs[wanted_name]

            result = func(*args, **kwargs)  # Call the original function
            return result

        if hotkey in global_hotkeys:
            print(f"Warning: hotkey '{hotkey}' is already registered to "
                  f"{global_hotkeys[hotkey].__name__}, overwriting with {func.__name__}")
        global_hotkeys[the_hotkey] = wrapper

        return wrapper

    return actual_decorator

