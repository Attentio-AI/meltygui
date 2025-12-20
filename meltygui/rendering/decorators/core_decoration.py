import functools
import inspect
from typing import Any

from src.lsd.gl_gui.utils.glfw_utils import request_render

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

def defaults(*args, **kwargs):
    def decorator(cls):
        from src.lsd.gl_gui.view.core_views.core_meta import Meta
        new_meta = Meta(**kwargs)
        from src.lsd.gl_gui.melty import Melty
        Melty.type_defaults[cls] = new_meta
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
        from src.lsd.gl_gui.melty import Melty
        if obj not in Melty.live_attributes:
            Melty.live_attributes[obj] = set()
        if self.name not in Melty.live_attributes[obj]:
            Melty.live_attributes[obj].add(self.name)

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
        from src.lsd.gl_gui.melty import Melty
        if obj not in Melty.live_attributes:
            Melty.live_attributes[obj] = set()
        if self.name not in Melty.live_attributes[obj]:
            Melty.live_attributes[obj].add(self.name)

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
        excluded = getattr(obj, '__excluded_attrs__', set())
        deep_refresh_names = getattr(self, '__deep_refresh__', set())
        invalidate_all_flag = getattr(self, '__invalidate_all__', set())

        do_deep_refresh = self.name in deep_refresh_names
        visible = self.name not in excluded
        visible = visible or do_deep_refresh
        from src.lsd.gl_gui.melty import Melty

        if self.name in invalidate_all_flag:
            Melty.cache.invalidate_all()
            print(f"Invalidate all called due to change in {self.name}")
            request_render()
            return

        if old_value != new_value:
            if visible and not self.name.startswith('_') \
                    and self.name != "driver" and Melty.frame_count > 3:
                if do_deep_refresh:
                    Melty.cache.invalidate_up_by_obj(obj=obj, name=self.name, max_depth=2, force=True)
                    request_render()
                else:
                    Melty.cache.invalidate_up_by_obj(obj, self.name, max_depth=3)
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

def tint(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple, dict)):
            from_args = args[0]
            setattr(cls, '__tint__', from_args)
        return cls

    return decorator

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
            from src.lsd.gl_gui.model.core_model.draw_state import Hotkey
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
                from src.lsd.gl_gui.melty import Melty
                if wanted_name in Melty.global_attrs and wanted_name not in kwargs:
                    kwargs[wanted_name] = Melty.global_attrs[wanted_name]

            result = func(*args, **kwargs)  # Call the original function
            return result

        if hotkey in global_hotkeys:
            print(f"Warning: hotkey '{hotkey}' is already registered to "
                  f"{global_hotkeys[hotkey].__name__}, overwriting with {func.__name__}")
        global_hotkeys[the_hotkey] = wrapper

        return wrapper

    return actual_decorator

