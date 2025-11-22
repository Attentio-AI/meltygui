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


def live(cls):
    # Get excluded attributes from class (if defined)
    excluded = getattr(cls, '__excluded_attrs__', set())

    setattr(cls, '__melty__', True)

    all_keys = []
    for key in dir(cls):
        if key.startswith('__') and key.endswith('__'):
            continue

        if key not in excluded:
            all_keys.append(key)
    setattr(cls, '__all_attributes__', all_keys)

    # Add internal flag to excluded set
    init_flag = f'__{cls.__name__}_initializing__'
    excluded = excluded | {init_flag}

    # Store the original methods
    original_setattr = cls.__setattr__ if hasattr(cls, '__setattr__') else object.__setattr__
    original_init = cls.__init__

    @functools.wraps(original_setattr)
    def new_setattr(self, name: str, value: Any) -> None:
        # Set the attribute using the original __setattr__
        original_value = getattr(self, name, None)
        if original_setattr == object.__setattr__:
            object.__setattr__(self, name, value)
        else:
            original_setattr(self, name, value)

        # Check if we're initializing
        initializing = getattr(self, init_flag, False)

        deep_refresh_names = getattr(self, '__deep_refresh__', set())
        do_deep_refresh = name in deep_refresh_names
        visible = name not in excluded
        visible = visible or do_deep_refresh

        # Call invalidate() if:
        # - not currently initializing
        # - attribute is not excluded
        # - object has invalidate method
        from src.lsd.gl_gui.melty import Melty
        if value != original_value:
            if not initializing and visible and not name.startswith('_')\
                    and name != "driver" and Melty.frame_count > 3:
                if do_deep_refresh:
                    Melty.cache.invalidate_up_by_obj(obj=self, max_depth=4, force=True)
                    request_render()
                else:
                    Melty.cache.invalidate_up_by_obj(self, name)


    @functools.wraps(original_init)
    def new_init(self, *args, **kwargs):

        # Set initialization flag
        object.__setattr__(self, init_flag, True)
        try:
            # Call original __init__
            original_init(self, *args, **kwargs)
        finally:
            # Clear initialization flag
            object.__setattr__(self, init_flag, False)

    # Monkey patch the class
    cls.__setattr__ = new_setattr
    cls.__init__ = new_init

    return cls

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
            from src.lsd.gl_gui.model.core_model.new_core_model import Hotkey
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

